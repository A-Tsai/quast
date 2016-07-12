############################################################################
# Copyright (c) 2015-2016 Saint Petersburg State University
# Copyright (c) 2011-2015 Saint Petersburg Academic University
# All Rights Reserved
# See file LICENSE for details.
############################################################################
#
# Some comments in this script was kindly provided by Plantagora team and
# modified by QUAST team. It is not licensed under GPL as other parts of QUAST,
# but it can be distributed and used in QUAST pipeline with current remarks and
# citation. For more details about assess_assembly.pl please refer to
# http://www.plantagora.org website and to the following paper:
#
# Barthelson R, McFarlin AJ, Rounsley SD, Young S (2011) Plantagora: Modeling
# Whole Genome Sequencing and Assembly of Plant Genomes. PLoS ONE 6(12):
# e28436. doi:10.1371/journal.pone.0028436
############################################################################

from __future__ import with_statement
import os
import copy
import platform
import datetime
from itertools import repeat
from os.path import isfile, join

import fastaparser
import shutil
from libs import reporting, qconfig, qutils, ca_utils

from libs.log import get_logger
logger = get_logger(qconfig.LOGGER_DEFAULT_NAME)
from qutils import correct_name


def bin_fpath(fname):
    return join(ca_utils.contig_aligner_dirpath, fname)

ref_labels_by_chromosomes = {}
COMBINED_REF_FNAME = 'combined_reference.fasta'


class Misassembly:
    LOCAL = 0
    RELOCATION = 1
    TRANSLOCATION = 2
    INVERSION = 3
    INTERSPECTRANSLOCATION = 4  #for --meta, if translocation occurs between chromosomes of different references
    SCAFFOLD_GAP = 5
    FRAGMENTED = 6
    POTENTIALLY_MIS_CONTIGS = 7
    POTENTIALLY_MISASSEMBLIES = 8

class StructuralVariations(object):
    def __init__(self):
        self.inversions = []
        self.relocations = []
        self.translocations = []

    def get_count(self):
        return len(self.inversions) + len(self.relocations) + len(self.translocations)


class Mapping(object):
    def __init__(self, s1, e1, s2, e2, len1, len2, idy, ref, contig):
        self.s1, self.e1, self.s2, self.e2, self.len1, self.len2, self.idy, self.ref, self.contig = s1, e1, s2, e2, len1, len2, idy, ref, contig

    @classmethod
    def from_line(cls, line):
        # line from coords file,e.g.
        # 4324128  4496883  |   112426   285180  |   172755   172756  |  99.9900  | gi|48994873|gb|U00096.2|	NODE_333_length_285180_cov_221082
        line = line.split()
        assert line[2] == line[5] == line[8] == line[10] == '|', line
        contig = line[12]
        ref = line[11]
        s1, e1, s2, e2, len1, len2 = [int(line[i]) for i in [0, 1, 3, 4, 6, 7]]
        idy = float(line[9])
        return Mapping(s1, e1, s2, e2, len1, len2, idy, ref, contig)

    def __str__(self):
        return ' '.join(str(x) for x in [self.s1, self.e1, '|', self.s2, self.e2, '|', self.len1, self.len2, '|', self.idy, '|', self.ref, self.contig])

    def short_str(self):
        return ' '.join(str(x) for x in [self.s1, self.e1, '|', self.s2, self.e2, '|', self.len1, self.len2])

    def icarus_report_str(self, ambiguity=''):
        return '\t'.join(str(x) for x in [self.s1, self.e1, self.s2, self.e2, self.ref, self.contig, self.idy, ambiguity])

    def clone(self):
        return Mapping.from_line(str(self))

    def start(self):
        """Return start on contig (alsways <= end)"""
        return min(self.s2, self.e2)

    def end(self):
        """Return end on contig (alsways >= start)"""
        return max(self.s2, self.e2)


class IndelsInfo(object):
    def __init__(self):
        self.mismatches = 0
        self.insertions = 0
        self.deletions = 0
        self.indels_list = []

    def __add__(self, other):
        self.mismatches += other.mismatches
        self.insertions += other.insertions
        self.deletions += other.deletions
        self.indels_list += other.indels_list
        return self


def clear_files(fpath, nucmer_fpath):
    if qconfig.debug:
        return

    # delete temporary files
    for ext in ['.delta', '.coords_tmp', '.coords.headless']:
        if os.path.isfile(nucmer_fpath + ext):
            os.remove(nucmer_fpath + ext)


class NucmerStatus:
    FAILED = 0
    OK = 1
    NOT_ALIGNED = 2
    ERROR = 3


def run_nucmer(prefix, ref_fpath, contigs_fpath, log_out_fpath, log_err_fpath, index, emem_threads=1):
    # additional GAGE params of Nucmer: '-l', '30', '-banded'
    nucmer_cmdline = [bin_fpath('nucmer'), '-c', str(qconfig.min_cluster),
                      '-l', str(qconfig.min_cluster), '--maxmatch',
                      '-p', prefix]
    if ca_utils.is_emem_aligner():
        nucmer_cmdline += ['-t', str(emem_threads)]
    nucmer_cmdline += [ref_fpath, contigs_fpath]
    return_code = qutils.call_subprocess(nucmer_cmdline, stdout=open(log_out_fpath, 'a'), stderr=open(log_err_fpath, 'a'),
                                         indent='  ' + qutils.index_to_str(index))

    return return_code


def __fail(contigs_fpath, index):
    logger.error('  ' + qutils.index_to_str(index) +
                 'Failed aligning contigs ' + qutils.label_from_fpath(contigs_fpath) + ' to the reference. ' +
                 ('Run with the --debug flag to see additional information.' if not qconfig.debug else ''))
    return NucmerStatus.ERROR, {}, []


def create_nucmer_successful_check(fpath, contigs_fpath, ref_fpath):
    nucmer_successful_check_file = open(fpath, 'w')
    nucmer_successful_check_file.write("Assembly file size in bytes: %d\n" % os.path.getsize(contigs_fpath))
    nucmer_successful_check_file.write("Reference file size in bytes: %d\n" % os.path.getsize(ref_fpath))
    nucmer_successful_check_file.write("Successfully finished on " +
                                       datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S') + '\n')
    nucmer_successful_check_file.close()


def check_nucmer_successful_check(fpath, contigs_fpath, ref_fpath):
    successful_check_content = open(fpath).read().split('\n')
    if len(successful_check_content) < 2:
        return False
    if not successful_check_content[0].strip().endswith(str(os.path.getsize(contigs_fpath))):
        return False
    if not successful_check_content[1].strip().endswith(str(os.path.getsize(ref_fpath))):
        return False
    return True


def plantakolya(cyclic, index, contigs_fpath, nucmer_fpath, output_dirpath, ref_fpath, old_contigs_fpath, bed_fpath, parallel_by_chr, emem_threads):
    assembly_label = qutils.label_from_fpath_for_fname(contigs_fpath)

    logger.info('  ' + qutils.index_to_str(index) + assembly_label)

    # run plantakolya tool
    log_out_fpath = os.path.join(output_dirpath, qconfig.contig_report_fname_pattern % assembly_label + '.stdout')
    log_err_fpath = os.path.join(output_dirpath, qconfig.contig_report_fname_pattern % assembly_label + '.stderr')
    icarus_out_fpath = os.path.join(output_dirpath, qconfig.icarus_report_fname_pattern % assembly_label)
    misassembly_fpath = os.path.join(output_dirpath, qconfig.contig_report_fname_pattern % assembly_label + '.mis_contigs.info')
    planta_out_f = open(log_out_fpath, 'w')
    planta_err_f = open(log_err_fpath, 'w')
    icarus_out_f = open(icarus_out_fpath, 'w')
    icarus_header_cols = ['S1', 'E1', 'S2', 'E2', 'Reference', 'Contig', 'IDY', 'Ambiguous']
    print >> icarus_out_f, '\t'.join(icarus_header_cols)

    misassembly_file = open(misassembly_fpath, 'w')

    logger.info('  ' + qutils.index_to_str(index) + 'Logging to files ' + log_out_fpath +
                ' and ' + os.path.basename(log_err_fpath) + '...')
    maxun = 10
    epsilon = 0.99
    smgap = qconfig.extensive_misassembly_threshold
    umt = 0.5  # threshold for misassembled contigs with aligned less than $umt * 100% (Unaligned Missassembled Threshold)
    ort = 0.9  # threshold for skipping aligns that significantly overlaps with adjacent aligns (Overlap Relative Threshold)
    oat = 25   # threshold for skipping aligns that significantly overlaps with adjacent aligns (Overlap Absolute Threshold)
    odgap = 1000 # threshold for detecting aligns that significantly overlaps with adjacent aligns (Overlap Detecting Gap)
    nucmer_successful_check_fpath = nucmer_fpath + '.sf'
    coords_fpath = nucmer_fpath + '.coords'
    delta_fpath = nucmer_fpath + '.delta'
    filtered_delta_fpath = nucmer_fpath + '.fdelta'
    coords_filtered_fpath = nucmer_fpath + '.coords.filtered'
    unaligned_fpath = nucmer_fpath + '.unaligned'
    show_snps_fpath = nucmer_fpath + '.all_snps'
    used_snps_fpath = nucmer_fpath + '.used_snps'
    print >> planta_out_f, 'Aligning contigs to reference...'

    # Checking if there are existing previous nucmer alignments.
    # If they exist, using them to save time.
    using_existing_alignments = False
    if os.path.isfile(nucmer_successful_check_fpath) and\
       os.path.isfile(coords_fpath) and\
       (os.path.isfile(show_snps_fpath) or not qconfig.show_snps):
        if check_nucmer_successful_check(nucmer_successful_check_fpath, old_contigs_fpath, ref_fpath):
            print >> planta_out_f, '\tUsing existing alignments...'
            logger.info('  ' + qutils.index_to_str(index) + 'Using existing alignments... ')
            using_existing_alignments = True

    if not using_existing_alignments:
        print >> planta_out_f, '\tAligning contigs to the reference '
        logger.info('  ' + qutils.index_to_str(index) + 'Aligning contigs to the reference')
        nucmer_failed = False

        if not qconfig.splitted_ref:
            nucmer_exit_code = run_nucmer(nucmer_fpath, ref_fpath, contigs_fpath,
                                          log_out_fpath, log_err_fpath, index, emem_threads)
            if nucmer_exit_code != 0:
                return __fail(contigs_fpath, index)

        else:
            prefixes_and_chr_files = [(nucmer_fpath + "_" + os.path.basename(chr_fname), chr_fname)
                                      for chr_fname in qconfig.splitted_ref]

            # Daemonic processes are not allowed to have children,
            # so if we are already one of parallel processes
            # (i.e. daemonic) we can't start new daemonic processes
            if parallel_by_chr and not qconfig.memory_efficient:
                n_jobs = min(qconfig.max_threads, len(prefixes_and_chr_files))
                if emem_threads == 1:
                    emem_threads = max(int(qconfig.max_threads / n_jobs), 1)
            else:
                n_jobs = 1
            if n_jobs > 1:
                logger.info('    ' + 'Aligning to different chromosomes in parallel'
                                     ' (' + str(n_jobs) + ' threads)')

            # processing each chromosome separately (if we can)
            from joblib import Parallel, delayed
            nucmer_exit_codes = Parallel(n_jobs=n_jobs)(delayed(run_nucmer)(
                prefix, chr_file, contigs_fpath, log_out_fpath, log_err_fpath + "_part%d" % (i + 1), index, emem_threads)
                for i, (prefix, chr_file) in enumerate(prefixes_and_chr_files))

            print >> planta_err_f, "Stderr outputs for reference parts are in:"
            for i in range(len(prefixes_and_chr_files)):
                print >> planta_err_f, log_err_fpath + "_part%d" % (i + 1)
            print >> planta_err_f, ""

            if 0 not in nucmer_exit_codes:
                return __fail(contigs_fpath, index)

            else:
                # filling common delta file
                delta_file = open(delta_fpath, 'w')
                delta_file.write(ref_fpath + " " + contigs_fpath + "\n")
                delta_file.write("NUCMER\n")
                for i, (prefix, chr_fname) in enumerate(prefixes_and_chr_files):
                    if nucmer_exit_codes[i] != 0:
                        logger.warning('  ' + qutils.index_to_str(index) +
                        'Failed aligning contigs %s to reference part %s! Skipping this part. ' % (qutils.label_from_fpath(contigs_fpath),
                        chr_fname) + ('Run with the --debug flag to see additional information.' if not qconfig.debug else ''))
                        continue

                    chr_delta_fpath = prefix + '.delta'
                    if os.path.isfile(chr_delta_fpath):
                        chr_delta_file = open(chr_delta_fpath)
                        chr_delta_file.readline()
                        chr_delta_file.readline()
                        for line in chr_delta_file:
                            delta_file.write(line)
                        chr_delta_file.close()

                delta_file.close()

        # Filtering by IDY% = 95 (as GAGE did)
        return_code = qutils.call_subprocess(
            [bin_fpath('delta-filter'), '-i', '95', '-l', str(qconfig.min_alignment), delta_fpath],
            stdout=open(filtered_delta_fpath, 'w'),
            stderr=planta_err_f,
            indent='  ' + qutils.index_to_str(index))

        if return_code != 0:
            print >> planta_err_f, qutils.index_to_str(index) + 'Delta filter failed for', contigs_fpath, '\n'
            return __fail(contigs_fpath, index)

        shutil.move(filtered_delta_fpath, delta_fpath)

        tmp_coords_fpath = coords_fpath + '_tmp'

        return_code = qutils.call_subprocess(
            [bin_fpath('show-coords'), delta_fpath],
            stdout=open(tmp_coords_fpath, 'w'),
            stderr=planta_err_f,
            indent='  ' + qutils.index_to_str(index))
        if return_code != 0:
            print >> planta_err_f, qutils.index_to_str(index) + 'Show-coords failed for', contigs_fpath, '\n'
            return __fail(contigs_fpath, index)

        # removing waste lines from coords file
        coords_file = open(coords_fpath, 'w')
        header = []
        tmp_coords_file = open(tmp_coords_fpath)
        for line in tmp_coords_file:
            header.append(line)
            if line.startswith('====='):
                break
        coords_file.write(header[-2])
        coords_file.write(header[-1])
        for line in tmp_coords_file:
            coords_file.write(line)
        coords_file.close()
        tmp_coords_file.close()

        if not os.path.isfile(coords_fpath):
            print >> planta_err_f, qutils.index_to_str(index) + 'Alignment failed for', contigs_fpath + ':', coords_fpath, 'doesn\'t exist.'
            logger.info('  ' + qutils.index_to_str(index) + 'Alignment failed for ' + '\'' + assembly_label + '\'.')
            return NucmerStatus.FAILED, {}, []
        if len(open(coords_fpath).readlines()[-1].split()) < 13:
            print >> planta_err_f, qutils.index_to_str(index) + 'Nothing aligned for', contigs_fpath
            logger.info('  ' + qutils.index_to_str(index) + 'Nothing aligned for ' + '\'' + assembly_label + '\'.')
            return NucmerStatus.NOT_ALIGNED, {}, []

        if qconfig.show_snps:
            with open(coords_fpath) as coords_file:
                headless_coords_fpath = coords_fpath + '.headless'
                headless_coords_f = open(headless_coords_fpath, 'w')
                coords_file.readline()
                coords_file.readline()
                headless_coords_f.write(coords_file.read())
                headless_coords_f.close()
                headless_coords_f = open(headless_coords_fpath)

                return_code = qutils.call_subprocess(
                    [bin_fpath('show-snps'), '-S', '-T', '-H', delta_fpath],
                    stdin=headless_coords_f,
                    stdout=open(show_snps_fpath, 'w'),
                    stderr=planta_err_f,
                    indent='  ' + qutils.index_to_str(index))
                if return_code != 0:
                    print >> planta_err_f, qutils.index_to_str(index) + 'Show-snps failed for', contigs_fpath, '\n'
                    return __fail(contigs_fpath, index)

        create_nucmer_successful_check(nucmer_successful_check_fpath, old_contigs_fpath, ref_fpath)

    # Loading the alignment files
    print >> planta_out_f, 'Parsing coords...'
    aligns = {}
    coords_file = open(coords_fpath)
    coords_filtered_file = open(coords_filtered_fpath, 'w')
    coords_filtered_file.write(coords_file.readline())
    coords_filtered_file.write(coords_file.readline())
    sum_idy = 0.0
    num_idy = 0
    for line in coords_file:
        if line.strip() == '':
            break
        assert line[0] != '='
        #Clear leading spaces from nucmer output
        #Store nucmer lines in an array
        mapping = Mapping.from_line(line)
        sum_idy += mapping.idy
        num_idy += 1
        aligns.setdefault(mapping.contig, []).append(mapping)
    avg_idy = sum_idy / num_idy if num_idy else 0

    #### auxiliary functions ####
    def distance_between_alignments(align1, align2, pos_strand1, pos_strand2, cyclic_ref_len=None):
        # returns distance (in reference) between two alignments
        if pos_strand1 or pos_strand2:            # alignment 1 should be earlier in reference
            distance = align2.s1 - align1.e1 - 1
        else:                     # alignment 2 should be earlier in reference
            distance = align1.s1 - align2.e1 - 1
        cyclic_moment = False
        if cyclic_ref_len is not None:
            cyclic_distance = distance
            if align1.e1 < align2.e1 and (cyclic_ref_len - align2.e1 + align1.s1 - 1) < smgap:
                cyclic_distance += cyclic_ref_len * (-1 if pos_strand1 else 1)
            elif align1.e1 >= align2.e1 and (cyclic_ref_len - align1.e1 + align2.s1 - 1) < smgap:
                cyclic_distance += cyclic_ref_len * (1 if pos_strand1 else -1)
            if abs(cyclic_distance) < abs(distance):
                distance = cyclic_distance
                cyclic_moment = True
        return distance, cyclic_moment

    def is_misassembly(align1, align2, cyclic_ref_lens=None, contig_seq=None):
        #Calculate inconsistency between distances on the reference and on the contig
        distance_on_contig = min(align2.e2, align2.s2) - max(align1.e2, align1.s2) - 1
        if cyclic_ref_lens is not None and align1.ref == align2.ref:
            distance_on_reference, cyclic_moment = distance_between_alignments(align1, align2, align1.s2 < align1.e2,
                align2.s2 < align2.e2, cyclic_ref_lens[align1.ref])
        else:
            distance_on_reference, cyclic_moment = distance_between_alignments(align1, align2, align1.s2 < align1.e2,
                                                                               align2.s2 < align2.e2,)

        misassembly_internal_overlap = 0
        if distance_on_contig < 0:
            if distance_on_reference >= 0:
                misassembly_internal_overlap = (-distance_on_contig)
            elif (-distance_on_reference) < (-distance_on_contig):
                misassembly_internal_overlap = (distance_on_reference - distance_on_contig)

        strand1 = (align1.s2 < align1.e2)
        strand2 = (align2.s2 < align2.e2)
        inconsistency = distance_on_reference - distance_on_contig
        if qconfig.scaffolds and contig_seq and check_is_scaffold_gap(inconsistency, seq, align1, align2):
            aux_data = {"inconsistency": inconsistency, "distance_on_contig": distance_on_contig,
                        "misassembly_internal_overlap": misassembly_internal_overlap, "cyclic_moment": cyclic_moment,
                        "is_sv": False, "is_translocation": False, "is_scaffold_gap": True}
            return False, aux_data
        # check for fake translocations (if reference is fragmented)
        is_translocation = False
        if align1.ref != align2.ref:
            if qconfig.is_combined_ref and \
                    not check_chr_for_refs(align1.ref, align2.ref):
                is_translocation = True
            elif qconfig.check_for_fragmented_ref:
                distance_on_reference = [min(abs(align.e1 - len(references[align.ref])),  abs(align.s1 - 1))
                                         for align in [align1, align2]]
                if all([d <= qconfig.MAX_INDEL_LENGTH for d in distance_on_reference]):
                    inconsistency = sum(distance_on_reference)
                    strand1 = strand2
                else:
                    is_translocation = True
            else:
                is_translocation = True
        aux_data = {"inconsistency": inconsistency, "distance_on_contig": distance_on_contig,
                    "misassembly_internal_overlap": misassembly_internal_overlap, "cyclic_moment": cyclic_moment,
                    "is_sv": False, "is_translocation": is_translocation, "is_scaffold_gap": False}
        if region_struct_variations:
            #check if it is structural variation
            is_sv = check_sv(align1, align2, inconsistency, region_struct_variations)
            if is_sv:
                aux_data['is_sv'] = True
                return False, aux_data

        # different chromosomes or large inconsistency (a gap or an overlap) or different strands
        if align1.ref != align2.ref and not is_translocation:
            return False, aux_data
        if align1.ref != align2.ref or \
                        abs(inconsistency) > smgap or (strand1 != strand2):
            return True, aux_data
        else:
            return False, aux_data

    def check_sv(align1, align2, inconsistency, region_struct_variations):
        max_error = 100 # smgap / 4  # min(2 * smgap, max(smgap, inconsistency * 0.05))
        max_gap = smgap / 4

        def __match_ci(pos, sv):  # check whether pos matches confidence interval of sv
            return sv.s1 - max_error <= pos <= sv.e1 + max_error

        if align2.s1 < align1.s1:
            align1, align2 = align2, align1
        if align1.ref != align2.ref:  # translocation
            for sv in region_struct_variations.translocations:
                if sv[0].ref == align1.ref and sv[1].ref == align2.ref and \
                        __match_ci(align1.e1, sv[0]) and __match_ci(align2.s1, sv[1]):
                    return True
                if sv[0].ref == align2.ref and sv[1].ref == align1.ref and \
                        __match_ci(align2.e1, sv[0]) and __match_ci(align1.s1, sv[1]):
                    return True
        elif (align1.s2 < align1.e2) != (align2.s2 < align2.e2) and abs(inconsistency) < smgap:
            for sv in region_struct_variations.inversions:
                if align1.ref == sv[0].ref and \
                        (__match_ci(align1.s1, sv[0]) and __match_ci(align2.s1, sv[1])) or \
                        (__match_ci(align1.e1, sv[0]) and __match_ci(align2.e1, sv[1])):
                    return True
        else:
            variations = region_struct_variations.relocations
            for index, sv in enumerate(variations):
                if sv[0].ref == align1.ref and __match_ci(align1.e1, sv[0]):
                    if __match_ci(align2.s1, sv[1]):
                        return True
                    # unite large deletion (relocations only)
                    prev_end = sv[1].e1
                    index_variation = index + 1
                    while index_variation < len(variations) and \
                                            variations[index_variation][0].s1 - prev_end <= max_gap and \
                                            variations[index_variation][0].ref == align1.ref:
                        sv = variations[index_variation]
                        if __match_ci(align2.s1, sv[1]):
                            return True
                        prev_end = sv[1].e1
                        index_variation += 1
        return False

    def find_all_sv(bed_fpath):
        if not bed_fpath:
            return None
        region_struct_variations = StructuralVariations()
        f = open(bed_fpath)
        for line in f:
            l = line.split('\t')
            if len(l) > 6 and not line.startswith('#'):
                try:
                    align1 = Mapping(s1=int(l[1]), e1=int(l[2]), ref=correct_name(l[0]), s2=None, e2=None, len1=None, len2=None, idy=None, contig=None)
                    align2 = Mapping(s1=int(l[4]), e1=int(l[5]),  ref=correct_name(l[3]), s2=None, e2=None, len1=None, len2=None, idy=None, contig=None)
                    if align1.ref != align2.ref:
                        region_struct_variations.translocations.append((align1, align2))
                    elif 'INV' in l[6]:
                        region_struct_variations.inversions.append((align1, align2))
                    elif 'DEL' in l[6]:
                        region_struct_variations.relocations.append((align1, align2))
                    else:
                        pass # not supported yet
                except ValueError:
                    pass  # incorrect line format
        return region_struct_variations

    def check_is_scaffold_gap(inconsistency, contig_seq, align1, align2):
        if abs(inconsistency) <= qconfig.scaffolds_gap_threshold and align1.ref == align2.ref and \
                is_gap_filled_ns(contig_seq, align1, align2) and (align1.s2 < align1.e2) == (align2.s2 < align2.e2):
            return True
        return False

    def exclude_internal_overlaps(align1, align2, i):
        # returns size of align1.len2 decrease (or 0 if not changed). It is important for cur_aligned_len calculation

        def __shift_start(align, new_start, indent=''):
            print >> planta_out_f, indent + '%s' % align.short_str(),
            if align.s2 < align.e2:
                align.s1 += (new_start - align.s2)
                align.s2 = new_start
                align.len2 = align.e2 - align.s2 + 1
            else:
                align.e1 -= (new_start - align.e2)
                align.e2 = new_start
                align.len2 = align.s2 - align.e2 + 1
            align.len1 = align.e1 - align.s1 + 1
            print >> planta_out_f, '--> %s' % align.short_str()

        def __shift_end(align, new_end, indent=''):
            print >> planta_out_f, indent + '%s' % align.short_str(),
            if align.s2 < align.e2:
                align.e1 -= (align.e2 - new_end)
                align.e2 = new_end
                align.len2 = align.e2 - align.s2 + 1
            else:
                align.s1 += (align.s2 - new_end)
                align.s2 = new_end
                align.len2 = align.s2 - align.e2 + 1
            align.len1 = align.e1 - align.s1 + 1
            print >> planta_out_f, '--> %s' % align.short_str()

        if qconfig.ambiguity_usage == 'all':
            return 0

        distance_on_contig = min(align2.e2, align2.s2) - max(align1.e2, align1.s2) - 1
        if distance_on_contig >= 0:  # no overlap
            return 0
        prev_len2 = align1.len2
        print >> planta_out_f, '\t\t\tExcluding internal overlap of size %d between Alignment %d and %d: ' \
                               % (-distance_on_contig, i+1, i+2),
        if qconfig.ambiguity_usage == 'one':  # left only one of two copies (remove overlap from shorter alignment)
            if align1.len2 >= align2.len2:
                __shift_start(align2, max(align1.e2, align1.s2) + 1)
            else:
                __shift_end(align1, min(align2.e2, align2.s2) - 1)
        else:  # ambiguity_usage == 'none':  removing both copies
            print >> planta_out_f
            new_end = min(align2.e2, align2.s2) - 1
            __shift_start(align2, max(align1.e2, align1.s2) + 1, '\t\t\t  ')
            __shift_end(align1, new_end, '\t\t\t  ')
        return prev_len2 - align1.len2

    def check_chr_for_refs(chr1, chr2):
        return ref_labels_by_chromosomes[chr1] == ref_labels_by_chromosomes[chr2]

    def count_not_ns_between_aligns(contig_seq, align1, align2):
        gap_in_contig = contig_seq[max(align1.e2, align1.s2): min(align2.e2, align2.s2) - 1]
        return len(gap_in_contig) - gap_in_contig.count('N')

    def is_gap_filled_ns(contig_seq, align1, align2):
        gap_in_contig = contig_seq[max(align1.e2, align1.s2): min(align2.e2, align2.s2) - 1]
        if len(gap_in_contig) < qconfig.Ns_break_threshold:
            return False
        return gap_in_contig.count('N')/len(gap_in_contig) > 0.95

    def add_potential_misassembly(ref):
        cur_ref = ref_labels_by_chromosomes[ref]
        total_misassemblies_by_refs[cur_ref][Misassembly.POTENTIALLY_MISASSEMBLIES] += 1
        region_misassemblies.append(Misassembly.POTENTIALLY_MISASSEMBLIES)

    def calculate_unaligned_part(seq, align, is_intermediate_align=False):
        unaligned_part = seq
        unaligned_len = len(unaligned_part)
        count_ns = unaligned_part.count('N')
        possible_misassembly = 0
        if count_ns / float(unaligned_len) < 0.95 and unaligned_len - count_ns >= qconfig.significant_part_size:
            possible_misassembly = 1
            add_potential_misassembly(align.ref)
            if is_intermediate_align:
                add_potential_misassembly(align.ref)
        return possible_misassembly, unaligned_len, count_ns

    def check_for_potential_translocation(seq, ctg_len, sorted_aligns):
        total_count_ns = 0
        total_unaligned_len = 0
        prev_end = 1
        misassemblies_count = 0
        for i, align in enumerate(sorted_aligns):
            if align.start() > prev_end + 1:
                possible_misassembly, unaligned_len, count_ns = calculate_unaligned_part(seq[prev_end + 1: align.start()],
                                                                                         align, is_intermediate_align=i > 0)
                misassemblies_count += possible_misassembly
                total_unaligned_len += unaligned_len
                total_count_ns += count_ns
            prev_end = align.end()
        if ctg_len > prev_end + 1:
            possible_misassembly, unaligned_len, count_ns = calculate_unaligned_part(seq[prev_end + 1: ctg_len], sorted_aligns[-1])
            misassemblies_count += possible_misassembly
            total_unaligned_len += unaligned_len
            total_count_ns += count_ns
        if not misassemblies_count:
            return

        # total_misassemblies_by_refs[cur_ref][Misassembly.POTENTIALLY_MIS_CONTIGS] += 1
        region_misassemblies.append(Misassembly.POTENTIALLY_MIS_CONTIGS)
        print >> planta_out_f, '\t\tIt can contain interspecies translocations'

    def process_misassembled_contig(sorted_aligns, cyclic, aligned_lengths, region_misassemblies, reg_lens, ref_aligns,
                                    ref_features, contig_seq, references_misassemblies, region_struct_variations, misassemblies_matched_sv, total_misassemblies_by_refs):
        misassembly_internal_overlap = 0
        prev = sorted_aligns[0]
        cur_aligned_length = prev.len2
        is_misassembled = False
        contig_is_printed = False
        indels_info = IndelsInfo()
        contig_aligned_length = 0  # for internal debugging purposes
        next_align = sorted_aligns[0]

        for i in range(len(sorted_aligns) - 1):
            is_extensive_misassembly, aux_data = is_misassembly(sorted_aligns[i], sorted_aligns[i+1],
                reg_lens if cyclic else None, contig_seq)
            inconsistency = aux_data["inconsistency"]
            distance_on_contig = aux_data["distance_on_contig"]
            misassembly_internal_overlap += aux_data["misassembly_internal_overlap"]
            cyclic_moment = aux_data["cyclic_moment"]
            is_translocation = aux_data["is_translocation"]
            print >> icarus_out_f, next_align.icarus_report_str()
            next_align = copy.deepcopy(sorted_aligns[i + 1])
            if sorted_aligns[i].ref == sorted_aligns[i+1].ref or (sorted_aligns[i].ref != sorted_aligns[i+1].ref and is_translocation):
                cur_aligned_length -= exclude_internal_overlaps(sorted_aligns[i], sorted_aligns[i+1], i)
            is_sv = aux_data["is_sv"]

            print >> planta_out_f, '\t\t\tReal Alignment %d: %s' % (i+1, str(sorted_aligns[i]))

            ref_aligns.setdefault(sorted_aligns[i].ref, []).append(sorted_aligns[i])
            print >> coords_filtered_file, str(prev)
            if is_sv:
                print >> planta_out_f, '\t\t\t  Fake misassembly (caused by structural variations of genome) between these two alignments'
                print >> icarus_out_f, 'fake misassembly (structural variations of genome)'
                misassemblies_matched_sv += 1

            elif qconfig.scaffolds and aux_data["is_scaffold_gap"]:
                print >> planta_out_f, '\t\t\t  Fake misassembly between these two alignments: scaffold gap size misassembly,',
                print >> planta_out_f, 'gap length difference =', inconsistency
                region_misassemblies.append(Misassembly.SCAFFOLD_GAP)
                print >> icarus_out_f, 'fake misassembly (scaffold gap size misassembly)'

            elif is_extensive_misassembly and not is_sv:
                is_misassembled = True
                aligned_lengths.append(cur_aligned_length)
                contig_aligned_length += cur_aligned_length
                cur_aligned_length = 0
                if not contig_is_printed:
                    print >> misassembly_file, sorted_aligns[i].contig
                    contig_is_printed = True
                print >> misassembly_file, 'Extensive misassembly (',
                print >> planta_out_f, '\t\t\t  Extensive misassembly (',
                if sorted_aligns[i].ref != sorted_aligns[i+1].ref and is_translocation:
                    if qconfig.is_combined_ref and \
                            not check_chr_for_refs(sorted_aligns[i].ref, sorted_aligns[i+1].ref):  # if chromosomes from different references
                            region_misassemblies.append(Misassembly.INTERSPECTRANSLOCATION)
                            ref1, ref2 = ref_labels_by_chromosomes[sorted_aligns[i].ref], ref_labels_by_chromosomes[sorted_aligns[i+1].ref]
                            references_misassemblies[ref1][ref2] += 1
                            references_misassemblies[ref2][ref1] += 1
                            total_misassemblies_by_refs[ref1][Misassembly.INTERSPECTRANSLOCATION] += 1
                            total_misassemblies_by_refs[ref2][Misassembly.INTERSPECTRANSLOCATION] += 1
                            print >> planta_out_f, 'interspecies translocation',
                            print >> misassembly_file, 'interspecies translocation',
                            print >> icarus_out_f, 'interspecies translocation'
                    else:
                        region_misassemblies.append(Misassembly.TRANSLOCATION)
                        if ref_labels_by_chromosomes:
                            cur_ref = ref_labels_by_chromosomes[sorted_aligns[i].ref]
                            total_misassemblies_by_refs[cur_ref][Misassembly.TRANSLOCATION] += 1
                        print >> planta_out_f, 'translocation',
                        print >> misassembly_file, 'translocation',
                        print >> icarus_out_f, 'translocation'
                elif abs(inconsistency) > smgap:
                    region_misassemblies.append(Misassembly.RELOCATION)
                    if ref_labels_by_chromosomes:
                        cur_ref = ref_labels_by_chromosomes[sorted_aligns[i].ref]
                        total_misassemblies_by_refs[cur_ref][Misassembly.RELOCATION] += 1
                    print >> planta_out_f, 'relocation, inconsistency =', inconsistency,
                    print >> misassembly_file, 'relocation, inconsistency =', inconsistency,
                    print >> icarus_out_f, 'relocation, inconsistency =', inconsistency
                else: #if strand1 != strand2:
                    region_misassemblies.append(Misassembly.INVERSION)
                    if ref_labels_by_chromosomes:
                        cur_ref = ref_labels_by_chromosomes[sorted_aligns[i].ref]
                        total_misassemblies_by_refs[cur_ref][Misassembly.INVERSION] += 1
                    print >> planta_out_f, 'inversion',
                    print >> misassembly_file, 'inversion',
                    print >> icarus_out_f, 'inversion'
                print >> planta_out_f, ') between these two alignments'
                print >> misassembly_file, ') between %s %s and %s %s' % (sorted_aligns[i].s2, sorted_aligns[i].e2,
                                                                          sorted_aligns[i+1].s2, sorted_aligns[i+1].e2)
                ref_features.setdefault(sorted_aligns[i].ref, {})[sorted_aligns[i].e1] = 'M'
                ref_features.setdefault(sorted_aligns[i+1].ref, {})[sorted_aligns[i+1].e1] = 'M'

            elif not is_sv:
                if inconsistency == 0 and cyclic_moment:
                    print >> planta_out_f, '\t\t\t  Fake misassembly (caused by linear representation of circular genome) between these two alignments'
                    print >> icarus_out_f, 'fake misassembly (linear representation of circular genome)'
                elif qconfig.check_for_fragmented_ref and sorted_aligns[i].ref != sorted_aligns[i+1].ref and not is_translocation:
                    print >> planta_out_f, '\t\t\t  Fake misassembly (caused by fragmentation of reference genome) between these two alignments'
                    region_misassemblies.append(Misassembly.FRAGMENTED)
                    print >> icarus_out_f, 'fake misassembly (fragmentation of reference genome)'
                elif abs(inconsistency) <= qconfig.MAX_INDEL_LENGTH and \
                        count_not_ns_between_aligns(contig_seq, sorted_aligns[i], sorted_aligns[i+1]) <= qconfig.MAX_INDEL_LENGTH:
                    print >> planta_out_f, '\t\t\t  Fake misassembly between these two alignments: inconsistency =', inconsistency,
                    print >> planta_out_f, ', gap in the contig is small or absent or filled mostly with Ns',
                    not_ns_number = count_not_ns_between_aligns(contig_seq, sorted_aligns[i], sorted_aligns[i+1])
                    if inconsistency == 0:
                        print >> planta_out_f, '(no indel; %d mismatches)' % not_ns_number
                        indels_info.mismatches += not_ns_number
                    else:
                        indel_length = abs(inconsistency)
                        indel_class = 'short' if indel_length <= qconfig.SHORT_INDEL_THRESHOLD else 'long'
                        indel_type = 'insertion' if inconsistency < 0 else 'deletion'
                        mismatches = max(0, not_ns_number - indel_length)
                        print >> planta_out_f, '(%s indel: %s of length %d; %d mismatches)' % \
                                               (indel_class, indel_type, indel_length, mismatches)
                        indels_info.indels_list.append(indel_length)
                        if indel_type == 'insertion':
                            indels_info.insertions += indel_length
                        else:
                            indels_info.deletions += indel_length
                        indels_info.mismatches += mismatches
                    print >> icarus_out_f, 'fake misassembly (gap in the contig is small or filled with Ns)'
                else:
                    if qconfig.strict_NA:
                        aligned_lengths.append(cur_aligned_length)
                        contig_aligned_length += cur_aligned_length
                        cur_aligned_length = 0

                    if inconsistency < 0:
                        #There is an overlap between the two alignments, a local misassembly
                        print >> planta_out_f, '\t\t\t  Overlap between these two alignments (local misassembly).',
                    else:
                        #There is a small gap between the two alignments, a local misassembly
                        print >> planta_out_f, '\t\t\t  Gap between these two alignments (local misassembly).',
                        #print >> plantafile_out, 'Distance on contig =', distance_on_contig, ', distance on reference =', distance_on_reference
                    print >> planta_out_f, 'Inconsistency =', inconsistency, "(linear representation of circular genome)" if cyclic_moment else "",\
                        "(fragmentation of reference genome)" if sorted_aligns[i].ref != sorted_aligns[i+1].ref else ""
                    print >> icarus_out_f, 'local misassembly'
                    region_misassemblies.append(Misassembly.LOCAL)

            prev = sorted_aligns[i+1]
            cur_aligned_length += prev.len2 - (-distance_on_contig if distance_on_contig < 0 else 0)

        #Record the very last alignment
        i = len(sorted_aligns) - 1
        print >> planta_out_f, '\t\t\tReal Alignment %d: %s' % (i + 1, str(sorted_aligns[i]))
        print >> icarus_out_f, next_align.icarus_report_str()
        ref_aligns.setdefault(sorted_aligns[i].ref, []).append(sorted_aligns[i])
        print >> coords_filtered_file, str(prev)
        aligned_lengths.append(cur_aligned_length)
        contig_aligned_length += cur_aligned_length

        assert contig_aligned_length <= len(contig_seq), "Internal QUAST bug: contig aligned length is greater than " \
                                                         "contig length (contig: %s, len: %d, aligned: %d)!" % \
                                                         (sorted_aligns[0].contig, contig_aligned_length, len(contig_seq))

        return is_misassembled, misassembly_internal_overlap, references_misassemblies, indels_info, misassemblies_matched_sv, total_misassemblies_by_refs
    #### end of aux. functions ###

    # Loading the reference sequences
    print >> planta_out_f, 'Loading reference...'  # TODO: move up
    references = {}
    ref_aligns = {}
    ref_features = {}
    for name, seq in fastaparser.read_fasta(ref_fpath):
        name = name.split()[0]  # no spaces in reference header
        references[name] = seq
        print >> planta_out_f, '\tLoaded [%s]' % name

    #Loading the SNP calls
    if qconfig.show_snps:
        print >> planta_out_f, 'Loading SNPs...'

    class SNP():
        def __init__(self, ref=None, ctg=None, ref_pos=None, ctg_pos=None, ref_nucl=None, ctg_nucl=None):
            self.ref_pos = ref_pos
            self.ctg_pos = ctg_pos
            self.ref_nucl = ref_nucl
            self.ctg_nucl = ctg_nucl
            self.type = 'I' if self.ref_nucl == '.' else ('D' if ctg_nucl == '.' else 'S')

    if qconfig.show_snps:
        snps = {}
        prev_line = None
        for line in open(show_snps_fpath):
            #print "$line";
            line = line.split()
            if not line[0].isdigit():
                continue
            if prev_line and line == prev_line:
                continue
            ref = line[10]
            ctg = line[11]
            pos = int(line[0]) # Kolya: python don't convert int<->str types automatically
            loc = int(line[3]) # Kolya: same as above

            # if (! exists $line[11]) { die "Malformed line in SNP file.  Please check that show-snps has completed succesfully.\n$line\n[$line[9]][$line[10]][$line[11]]\n"; }
            if pos in snps.setdefault(ref, {}).setdefault(ctg, {}):
                snps.setdefault(ref, {}).setdefault(ctg, {})[pos].append(SNP(ref_pos=pos, ctg_pos=loc, ref_nucl=line[1], ctg_nucl=line[2]))
            else:
                snps.setdefault(ref, {}).setdefault(ctg, {})[pos] = [SNP(ref_pos=pos, ctg_pos=loc, ref_nucl=line[1], ctg_nucl=line[2])]
            prev_line = line
        used_snps_file = open(used_snps_fpath, 'w')

    # Loading the regions (if any)
    regions = {}
    reg_lens = {}
    total_reg_len = 0
    total_regions = 0
    print >> planta_out_f, 'Loading regions...'
    # TODO: gff
    print >> planta_out_f, '\tNo regions given, using whole reference.'
    for name, seq in references.iteritems():
        regions.setdefault(name, []).append([1, len(seq)])
        reg_lens[name] = len(seq)
        total_regions += 1
        total_reg_len += len(seq)
    print >> planta_out_f, '\tTotal Regions: %d' % total_regions
    print >> planta_out_f, '\tTotal Region Length: %d' % total_reg_len

    unaligned = 0
    partially_unaligned = 0
    fully_unaligned_bases = 0
    partially_unaligned_bases = 0
    ambiguous_contigs = 0
    ambiguous_contigs_extra_bases = 0
    uncovered_regions = 0
    uncovered_region_bases = 0
    total_redundant = 0
    partially_unaligned_with_misassembly = 0
    partially_unaligned_with_significant_parts = 0
    misassembly_internal_overlap = 0

    region_misassemblies = []
    misassembled_contigs = {}
    references_misassemblies = {}
    for ref in ref_labels_by_chromosomes.values():
        references_misassemblies[ref] = dict((key, 0) for key in ref_labels_by_chromosomes.values())
    total_misassemblies_by_refs = {}
    for ref in ref_labels_by_chromosomes.values():
        total_misassemblies_by_refs[ref] = [0] * (Misassembly.POTENTIALLY_MISASSEMBLIES + 1)


    aligned_lengths = []

    misassemblies_matched_sv = 0
    region_struct_variations = find_all_sv(bed_fpath)

    # for counting SNPs and indels (both original (.all_snps) and corrected from Nucmer's local misassemblies)
    total_indels_info = IndelsInfo()

    print >> planta_out_f, 'Analyzing contigs...'

    unaligned_file = open(unaligned_fpath, 'w')
    for contig, seq in fastaparser.read_fasta(contigs_fpath):
        contig_ns = None
        if 'N' in seq:
            contig_ns = [pos for pos in xrange(len(seq)) if seq[pos] == 'N']
        #Recording contig stats
        ctg_len = len(seq)
        print >> planta_out_f, 'CONTIG: %s (%dbp)' % (contig, ctg_len)
        contig_type = 'unaligned'

        #Check if this contig aligned to the reference
        if contig in aligns:
            contig_type = 'correct'
            #Pull all aligns for this contig
            num_aligns = len(aligns[contig])

            #Sort aligns by length and identity
            sorted_aligns = sorted(aligns[contig], key=lambda x: (x.len2 * x.idy, x.len2), reverse=True)
            top_len = sorted_aligns[0].len2
            top_id = sorted_aligns[0].idy
            top_aligns = []
            print >> planta_out_f, 'Top Length: %s  Top ID: %s' % (top_len, top_id)

            #Check that top hit captures most of the contig
            if top_len > ctg_len * epsilon or ctg_len - top_len < maxun:
                #Reset top aligns: aligns that share the same value of longest and highest identity
                top_aligns.append(sorted_aligns[0])
                sorted_aligns = sorted_aligns[1:]

                #Continue grabbing alignments while length and identity are identical
                #while sorted_aligns and top_len == sorted_aligns[0].len2 and top_id == sorted_aligns[0].idy:
                while sorted_aligns and ((sorted_aligns[0].len2 * sorted_aligns[0].idy) / (top_len * top_id) > epsilon):
                    top_aligns.append(sorted_aligns[0])
                    sorted_aligns = sorted_aligns[1:]

                #Mark other alignments as ambiguous
                while sorted_aligns:
                    ambig = sorted_aligns.pop()
                    print >> planta_out_f, '\t\tMarking as insignificant: %s' % str(ambig) # former ambiguous
                    # Kolya: removed redundant code about $ref (for gff AFAIU)

                if len(top_aligns) == 1:
                    #There is only one top align, life is good
                    print >> planta_out_f, '\t\tOne align captures most of this contig: %s' % str(top_aligns[0])
                    print >> icarus_out_f, top_aligns[0].icarus_report_str()
                    ref_aligns.setdefault(top_aligns[0].ref, []).append(top_aligns[0])
                    print >> coords_filtered_file, str(top_aligns[0])
                    aligned_lengths.append(top_aligns[0].len2)
                else:
                    #There is more than one top align
                    print >> planta_out_f, '\t\tThis contig has %d significant alignments. [An ambiguously mapped contig]' % len(
                        top_aligns)

                    #Increment count of ambiguously mapped contigs and bases in them
                    ambiguous_contigs += 1
                    # we count only extra bases, so we shouldn't include bases in the first alignment
                    # in case --allow-ambiguity is not set the number of extra bases will be negative!
                    ambiguous_contigs_extra_bases -= top_aligns[0].len2

                    # Alex: skip all alignments or count them as normal (just different aligns of one repeat). Depend on --allow-ambiguity option
                    if qconfig.ambiguity_usage == "none":
                        print >> planta_out_f, '\t\tSkipping these alignments (option --ambiguity-usage is set to "none"):'
                        for align in top_aligns:
                            print >> planta_out_f, '\t\tSkipping alignment ', align
                    elif qconfig.ambiguity_usage == "one":
                        print >> planta_out_f, '\t\tUsing only first of these alignment (option --ambiguity-usage is set to "one"):'
                        print >> planta_out_f, '\t\tAlignment: %s' % str(top_aligns[0])
                        print >> icarus_out_f, top_aligns[0].icarus_report_str()
                        ref_aligns.setdefault(top_aligns[0].ref, []).append(top_aligns[0])
                        aligned_lengths.append(top_aligns[0].len2)
                        print >> coords_filtered_file, str(top_aligns[0])
                        top_aligns = top_aligns[1:]
                        for align in top_aligns:
                            print >> planta_out_f, '\t\tSkipping alignment ', align
                    elif qconfig.ambiguity_usage == "all":
                        print >> planta_out_f, '\t\tUsing all these alignments (option --ambiguity-usage is set to "all"):'
                        # we count only extra bases, so we shouldn't include bases in the first alignment
                        first_alignment = True
                        while len(top_aligns):
                            print >> planta_out_f, '\t\tAlignment: %s' % str(top_aligns[0])
                            print >> icarus_out_f, top_aligns[0].icarus_report_str(ambiguity=True)
                            ref_aligns.setdefault(top_aligns[0].ref, []).append(top_aligns[0])
                            if first_alignment:
                                first_alignment = False
                                aligned_lengths.append(top_aligns[0].len2)
                            ambiguous_contigs_extra_bases += top_aligns[0].len2
                            print >> coords_filtered_file, str(top_aligns[0]), "ambiguous"
                            top_aligns = top_aligns[1:]

                    #Record these alignments as ambiguous on the reference
                    #                    for align in top_aligns:
                    #                        print >> plantafile_out, '\t\t\tAmbiguous Alignment: %s' % str(align)
                    #                        ref = align.ref
                    #                        for i in xrange(align.s1, align.e1+1):
                    #                            if (ref not in ref_features) or (i not in ref_features[ref]):
                    #                                ref_features.setdefault(ref, {})[i] = 'A'

                    #Increment count of ambiguous contigs and bases
                    #ambiguous += 1
                    #total_ambiguous += ctg_len
            else:
                # choose appropriate alignments (to maximize total size of contig alignment and reduce # misassemblies
                if len(sorted_aligns) > 0:
                    sorted_aligns = sorted(sorted_aligns, key=lambda x: x.end())

                    extensive_penalty = max(50, int(round(min(qconfig.extensive_misassembly_threshold / 4.0, ctg_len * 0.05)))) - 1
                    local_penalty = max(2, int(round(min(qconfig.MAX_INDEL_LENGTH / 2.0, ctg_len * 0.01)))) - 1
                    scaffold_gap_penalty = 5

                    critical_number_of_aligns = 200
                    # trying to optimise the algorithm if the number of possible alignments is large
                    if len(sorted_aligns) > critical_number_of_aligns:
                        print >> planta_out_f, '\t\t\tSkipping redundant alignments which can\'t be in the best set of alignments A PRIORI'

                        # FIRST STEP: find solid aligns (which are present in the best selection for sure)
                        # they should have unique (not covered by other aligns) region of length > 2 * extensive_penalty
                        min_unique_len = 2 * extensive_penalty

                        class PSA(object):  # PSA stands for Possibly Solid Alignment
                            def __init__(self, align):
                                self.align = align
                                self.unique_start = align.start()
                                self.unique_end = align.end()

                            def is_solid(self):
                                return self.unique_end - self.unique_start + 1 > min_unique_len

                            # intersect PSA with align, which is guaranteed to be inside of after this PSA
                            # return True if switch to the next PSA is needed and False otherwise
                            def intersect_and_go_next(self, align, solids):
                                if self.unique_end - align.end() > min_unique_len:  # if enough len on the right side
                                    if self.is_solid():
                                        solids.append(self.align)
                                        return True
                                self.unique_end = min(self.unique_end, align.start() - 1)
                                return not self.is_solid()  # if self is not solid anymore we can switch to the next PSA

                        possible_solids = [PSA(align) for align in sorted_aligns if align.len2 > min_unique_len]
                        solids = []
                        try:
                            cur_PSA = possible_solids.pop()
                            for align in reversed(sorted_aligns):
                                if align != cur_PSA.align and cur_PSA.intersect_and_go_next(align, solids):
                                    next_PSA = possible_solids.pop()
                                    while next_PSA.intersect_and_go_next(cur_PSA.align, solids):
                                        next_PSA = possible_solids.pop()
                                    while align != next_PSA.align and next_PSA.intersect_and_go_next(align, solids):
                                        next_PSA = possible_solids.pop()
                                    cur_PSA = next_PSA
                        except IndexError:  # possible_solids is empty
                            pass

                        # SECOND STEP: remove all aligns which are inside solid ones
                        if len(solids):
                            class SolidRegion(object):
                                def __init__(self, align):
                                    self.start = align.start()
                                    self.end = align.end()

                                def include(self, align):
                                    return self.start <= align.start() and align.end() <= self.end

                            solid_regions = []  # intersection of all solid aligns
                            cur_region = SolidRegion(solids[0])
                            for align in solids[1:]:
                                if align.end() + 1 < cur_region.start:
                                    solid_regions.append(cur_region)
                                    cur_region = SolidRegion(align)
                                else:  # shift start of the current region
                                    cur_region.start = align.start()
                            solid_regions.append(cur_region)

                            filtered_aligns = solids
                            idx = 0
                            try:
                                cur_region = solid_regions.pop()
                                for idx, align in enumerate(sorted_aligns):
                                    while not cur_region.include(align):
                                        if align.start() > cur_region.end:
                                            cur_region = solid_regions.pop()
                                            continue
                                        filtered_aligns.append(align)
                                        break
                                    else:
                                        print >> planta_out_f, '\t\tSkipping redundant alignment %s' % (str(align))
                            except IndexError:  # solid_regions is empty
                                filtered_aligns += sorted_aligns[idx:]

                            sorted_aligns = sorted(filtered_aligns, key=lambda x: x.end())

                    # auxiliary functions
                    def __get_added_len(set_aligns, cur_align):
                        last_align_idx = -2
                        last_align = set_aligns[last_align_idx]
                        added_right = cur_align.end() - max(cur_align.start(), last_align.end())
                        added_left = 0
                        while cur_align.start() < last_align.start():
                            added_left += last_align.start() - cur_align.start()
                            last_align_idx -= 1
                            if -last_align_idx <= len(set_aligns):
                                prev_start = last_align.start()  # in case of overlapping of old and new last_align
                                last_align = set_aligns[last_align_idx]
                                added_left -= max(0, min(prev_start, last_align.end()) - cur_align.start() + 1)
                            else:
                                break
                        return added_right + added_left

                    def __get_score(score, aligns, cyclic_ref_lens, uncovered_len):
                        if len(aligns) > 1:
                            added_len = __get_added_len(aligns, aligns[-1])
                            uncovered_len -= added_len
                            score += added_len
                            align1, align2 = aligns[-2], aligns[-1]
                            is_extensive_misassembly, aux_data = is_misassembly(align1, align2, cyclic_ref_lens, seq)
                            if is_extensive_misassembly:
                                score -= extensive_penalty
                            elif abs(aux_data['inconsistency']) > qconfig.MAX_INDEL_LENGTH and not aux_data['is_scaffold_gap']:
                                score -= local_penalty
                            elif aux_data['is_scaffold_gap']:
                                score -= scaffold_gap_penalty
                        else:
                            score += aligns[-1].len2
                            uncovered_len -= aligns[-1].len2
                        return score, uncovered_len

                    class ScoredAlignSet(object):
                        def __init__(self, score, indexes, uncovered):
                            self.score = score
                            self.indexes = indexes
                            self.uncovered = uncovered
                    # end of auxiliary functions

                    all_scored_sets = [ScoredAlignSet(0, [], ctg_len)]
                    max_score = 0
                    best_set = []

                    for idx, align in enumerate(sorted_aligns):
                        cur_max_score = 0
                        new_scored_set = None
                        sets_to_remove = []
                        for scored_set in all_scored_sets:
                            if (scored_set.score + align.len2) > cur_max_score:  # otherwise this set can't be the best with current align
                                cur_set_aligns = [sorted_aligns[i] for i in scored_set.indexes] + [align]
                                score, uncovered = __get_score(scored_set.score, cur_set_aligns,
                                                               reg_lens if cyclic else None, scored_set.uncovered)
                                if score + uncovered < max_score:
                                    sets_to_remove.append(scored_set)
                                elif score > cur_max_score:
                                    cur_max_score = score
                                    new_scored_set = ScoredAlignSet(score, scored_set.indexes + [idx], uncovered)
                        for bad_set in sets_to_remove:
                            all_scored_sets.remove(bad_set)
                        if new_scored_set:
                            all_scored_sets.append(new_scored_set)
                            if cur_max_score > max_score:
                                max_score = cur_max_score
                                best_set = new_scored_set.indexes

                    # save best selection to real aligns and skip others (as redundant)
                    real_aligns = list([sorted_aligns[i] for i in best_set])
                    if len(sorted_aligns) > len(real_aligns):
                        print >> planta_out_f, '\t\t\tSkipping redundant alignments after choosing the best set of alignments'
                        for align in sorted_aligns:
                            if align not in real_aligns:
                                print >> planta_out_f, '\t\tSkipping redundant alignment %s' % (str(align))

                if len(real_aligns) == 1:
                    the_only_align = real_aligns[0]

                    #There is only one alignment of this contig to the reference
                    print >> coords_filtered_file, str(the_only_align)
                    aligned_lengths.append(the_only_align.len2)

                    #Is the contig aligned in the reverse compliment?
                    #Record beginning and end of alignment in contig
                    if the_only_align.s2 > the_only_align.e2:
                        end, begin = the_only_align.s2, the_only_align.e2
                    else:
                        end, begin = the_only_align.e2, the_only_align.s2

                    if (begin - 1) or (ctg_len - end):
                        #Increment tally of partially unaligned contigs
                        partially_unaligned += 1

                        #Increment tally of partially unaligned bases
                        unaligned_bases = (begin - 1) + (ctg_len - end)
                        partially_unaligned_bases += unaligned_bases
                        print >> planta_out_f, '\t\tThis contig is partially unaligned. (Aligned %d out of %d bases)' % (top_len, ctg_len)
                        print >> planta_out_f, '\t\tAlignment: %s' % str(the_only_align)
                        print >> icarus_out_f, the_only_align.icarus_report_str()
                        if begin - 1:
                            print >> planta_out_f, '\t\tUnaligned bases: 1 to %d (%d)' % (begin - 1, begin - 1)
                        if ctg_len - end:
                            print >> planta_out_f, '\t\tUnaligned bases: %d to %d (%d)' % (end + 1, ctg_len, ctg_len - end)
                        # check if both parts (aligned and unaligned) have significant length
                        if (unaligned_bases >= qconfig.significant_part_size) and (ctg_len - unaligned_bases >= qconfig.significant_part_size):
                            partially_unaligned_with_significant_parts += 1
                            print >> planta_out_f, '\t\tThis contig has both significant aligned and unaligned parts ' \
                                                   '(of length >= %d)!' % (qconfig.significant_part_size)
                            if qconfig.meta and ref_labels_by_chromosomes:
                                check_for_potential_translocation(seq, ctg_len, real_aligns)

                    ref_aligns.setdefault(the_only_align.ref, []).append(the_only_align)
                else:
                    #Sort real alignments by position on the contig
                    sorted_aligns = sorted(real_aligns, key=lambda x: (min(x.s2, x.e2), max(x.s2, x.e2)))

                    #Extra skipping of redundant alignments (fully or almost fully covered by adjacent alignments)
                    if len(sorted_aligns) >= 3:
                        was_extra_skip = False
                        prev_end = max(sorted_aligns[0].s2, sorted_aligns[0].e2)
                        for i in range(1, len(sorted_aligns) - 1):
                            succ_start = min(sorted_aligns[i + 1].s2, sorted_aligns[i + 1].e2)
                            gap = succ_start - prev_end - 1
                            if gap > odgap:
                                prev_end = max(sorted_aligns[i].s2, sorted_aligns[i].e2)
                                continue
                            overlap = 0
                            if prev_end - min(sorted_aligns[i].s2, sorted_aligns[i].e2) + 1 > 0:
                                overlap += prev_end - min(sorted_aligns[i].s2, sorted_aligns[i].e2) + 1
                            if max(sorted_aligns[i].s2, sorted_aligns[i].e2) - succ_start + 1 > 0:
                                overlap += max(sorted_aligns[i].s2, sorted_aligns[i].e2) - succ_start + 1
                            if gap < oat or (float(overlap) / sorted_aligns[i].len2) > ort:
                                if not was_extra_skip:
                                    was_extra_skip = True
                                    print >> planta_out_f, '\t\t\tSkipping redundant alignments which significantly overlap with adjacent alignments'
                                print >> planta_out_f, '\t\tSkipping redundant alignment %s' % (str(sorted_aligns[i]))
                                real_aligns.remove(sorted_aligns[i])
                            else:
                                prev_end = max(sorted_aligns[i].s2, sorted_aligns[i].e2)
                        if was_extra_skip:
                            sorted_aligns = sorted(real_aligns, key=lambda x: (min(x.s2, x.e2), max(x.s2, x.e2)))

                    #There is more than one alignment of this contig to the reference
                    print >> planta_out_f, '\t\tThis contig is misassembled. %d total aligns.' % num_aligns

                    # Counting misassembled contigs which are mostly partially unaligned
                    # counting aligned and unaligned bases of a contig
                    aligned_bases_in_contig = 0
                    last_e2 = 0
                    for cur_align in sorted_aligns:
                        if max(cur_align.s2, cur_align.e2) <= last_e2:
                            continue
                        elif min(cur_align.s2, cur_align.e2) > last_e2:
                            aligned_bases_in_contig += (abs(cur_align.e2 - cur_align.s2) + 1)
                        else:
                            aligned_bases_in_contig += (max(cur_align.s2, cur_align.e2) - last_e2)
                        last_e2 = max(cur_align.s2, cur_align.e2)

                    #aligned_bases_in_contig = sum(x.len2 for x in sorted_aligns)
                    if aligned_bases_in_contig < umt * ctg_len:
                        print >> planta_out_f, '\t\t\tWarning! This contig is more unaligned than misassembled. ' + \
                            'Contig length is %d and total length of all aligns is %d' % (ctg_len, aligned_bases_in_contig)
                        partially_unaligned_with_misassembly += 1
                        for align in sorted_aligns:
                            print >> planta_out_f, '\t\tAlignment: %s' % str(align)
                            print >> icarus_out_f, align.icarus_report_str()
                            print >> coords_filtered_file, str(align)
                            aligned_lengths.append(align.len2)
                            ref_aligns.setdefault(align.ref, []).append(align)

                        #Increment tally of partially unaligned contigs
                        partially_unaligned += 1
                        #Increment tally of partially unaligned bases
                        partially_unaligned_bases += ctg_len - aligned_bases_in_contig
                        print >> planta_out_f, '\t\tUnaligned bases: %d' % (ctg_len - aligned_bases_in_contig)
                        # check if both parts (aligned and unaligned) have significant length
                        if (aligned_bases_in_contig >= qconfig.significant_part_size) and (ctg_len - aligned_bases_in_contig >= qconfig.significant_part_size):
                            partially_unaligned_with_significant_parts += 1
                            print >> planta_out_f, '\t\tThis contig has both significant aligned and unaligned parts ' \
                                                   '(of length >= %d)!' % (qconfig.significant_part_size)
                            if qconfig.meta and ref_labels_by_chromosomes:
                                check_for_potential_translocation(seq, ctg_len, sorted_aligns)
                        continue

                    ### processing misassemblies
                    is_misassembled, current_mio, references_misassemblies, indels_info, misassemblies_matched_sv, total_misassemblies_by_refs = process_misassembled_contig(sorted_aligns, cyclic,
                        aligned_lengths, region_misassemblies, reg_lens, ref_aligns, ref_features, seq, references_misassemblies, region_struct_variations, misassemblies_matched_sv, total_misassemblies_by_refs)
                    misassembly_internal_overlap += current_mio
                    total_indels_info += indels_info
                    if is_misassembled:
                        misassembled_contigs[contig] = len(seq)
                        contig_type = 'misassembled'
                    if aligned_bases_in_contig >= qconfig.significant_part_size and ctg_len - aligned_bases_in_contig >= qconfig.significant_part_size:
                        print >> planta_out_f, '\t\tThis contig has significant unaligned parts ' \
                                               '(of length >= %d)!' % (qconfig.significant_part_size)
                        if qconfig.meta and ref_labels_by_chromosomes:
                            check_for_potential_translocation(seq, ctg_len, sorted_aligns)
        else:
            #No aligns to this contig
            print >> planta_out_f, '\t\tThis contig is unaligned. (%d bp)' % ctg_len
            print >> unaligned_file, contig

            #Increment unaligned contig count and bases
            unaligned += 1
            fully_unaligned_bases += ctg_len
            print >> planta_out_f, '\t\tUnaligned bases: %d  total: %d' % (ctg_len, fully_unaligned_bases)

        print >> icarus_out_f, '\t'.join(['CONTIG', contig, str(ctg_len), contig_type])
        print >> planta_out_f

    coords_filtered_file.close()
    unaligned_file.close()

    print >> planta_out_f, 'Analyzing coverage...'
    if qconfig.show_snps:
        print >> planta_out_f, 'Writing SNPs into', used_snps_fpath

    region_covered = 0
    region_ambig = 0
    gaps = []
    neg_gaps = []
    redundant = []
    snip_left = 0
    snip_right = 0

    # for counting short and long indels
    # indels_list = []  # -- defined earlier
    prev_snp = None
    cur_indel = 0

    nothing_aligned = True
    #Go through each header in reference file
    for ref, value in regions.iteritems():
        #Check to make sure this reference ID contains aligns.
        if ref not in ref_aligns:
            print >> planta_out_f, 'ERROR: Reference %s does not have any alignments!  Check that this is the same file used for alignment.' % ref
            print >> planta_out_f, 'ERROR: Alignment Reference Headers: %s' % ref_aligns.keys()
            continue
        nothing_aligned = False

        #Sort all alignments in this reference by start location
        sorted_aligns = sorted(ref_aligns[ref], key=lambda x: x.s1)
        total_aligns = len(sorted_aligns)
        print >> planta_out_f, '\tReference %s: %d total alignments. %d total regions.' % (ref, total_aligns, len(regions[ref]))

        # the rest is needed for SNPs stats only
        if not qconfig.show_snps:
            continue

        #Walk through each region on this reference sequence
        for region in regions[ref]:
            end = 0
            reg_length = region[1] - region[0] + 1
            print >> planta_out_f, '\t\tRegion: %d to %d (%d bp)' % (region[0], region[1], reg_length)

            #Skipping alignments not in the next region
            while sorted_aligns and sorted_aligns[0].e1 < region[0]:
                skipped = sorted_aligns[0]
                sorted_aligns = sorted_aligns[1:] # Kolya: slooow, but should never happens without gff :)
                print >> planta_out_f, '\t\t\tThis align occurs before our region of interest, skipping: %s' % skipped

            if not sorted_aligns:
                print >> planta_out_f, '\t\t\tThere are no more aligns. Skipping this region.'
                continue

            #If region starts in a contig, ignore portion of contig prior to region start
            if sorted_aligns and region and sorted_aligns[0].s1 < region[0]:
                print >> planta_out_f, '\t\t\tSTART within alignment : %s' % sorted_aligns[0]
                #Track number of bases ignored at the start of the alignment
                snip_left = region[0] - sorted_aligns[0].s1
                #Modify to account for any insertions or deletions that are present
                for z in xrange(sorted_aligns[0].s1, region[0] + 1):
                    if (ref in snps) and (sorted_aligns[0].contig in snps[ref]) and (z in snps[ref][sorted_aligns[0].contig]) and \
                       (ref in ref_features) and (z in ref_features[ref]) and (ref_features[ref][z] != 'A'): # Kolya: never happened before because of bug: z -> i
                        for cur_snp in snps[ref][sorted_aligns[0].contig][z]:
                            if cur_snp.type == 'I':
                                snip_left += 1
                            elif cur_snp.type == 'D':
                                snip_left -= 1

                #Modify alignment to start at region
                print >> planta_out_f, '\t\t\t\tMoving reference start from %d to %d' % (sorted_aligns[0].s1, region[0])
                sorted_aligns[0].s1 = region[0]

                #Modify start position in contig
                if sorted_aligns[0].s2 < sorted_aligns[0].e2:
                    print >> planta_out_f, '\t\t\t\tMoving contig start from %d to %d.' % (sorted_aligns[0].s2, sorted_aligns[0].s2 + snip_left)
                    sorted_aligns[0].s2 += snip_left
                else:
                    print >> planta_out_f, '\t\t\t\tMoving contig start from %d to %d.' % (sorted_aligns[0].s2, sorted_aligns[0].s2 - snip_left)
                    sorted_aligns[0].s2 -= snip_left

            #No aligns in this region
            if sorted_aligns[0].s1 > region[1]:
                print >> planta_out_f, '\t\t\tThere are no aligns within this region.'
                gaps.append([reg_length, 'START', 'END'])
                #Increment uncovered region count and bases
                uncovered_regions += 1
                uncovered_region_bases += reg_length
                continue

            #Record first gap, and first ambiguous bases within it
            if sorted_aligns[0].s1 > region[0]:
                size = sorted_aligns[0].s1 - region[0]
                print >> planta_out_f, '\t\t\tSTART in gap: %d to %d (%d bp)' % (region[0], sorted_aligns[0].s1, size)
                gaps.append([size, 'START', sorted_aligns[0].contig])
                #Increment any ambiguously covered bases in this first gap
                for i in xrange(region[0], sorted_aligns[0].e1):
                    if (ref in ref_features) and (i in ref_features[ref]) and (ref_features[ref][i] == 'A'):
                        region_ambig += 1

            #For counting number of alignments
            counter = 0
            negative = False
            current = None
            while sorted_aligns and sorted_aligns[0].s1 < region[1] and not end:
                #Increment alignment count
                counter += 1
                if counter % 1000 == 0:
                    print >> planta_out_f, '\t...%d of %d' % (counter, total_aligns)
                end = False
                #Check to see if previous gap was negative
                if negative:
                    print >> planta_out_f, '\t\t\tPrevious gap was negative, modifying coordinates to ignore overlap'
                    #Ignoring OL part of next contig, no SNPs or N's will be recorded
                    snip_left = current.e1 + 1 - sorted_aligns[0].s1
                    #Account for any indels that may be present
                    for z in xrange(sorted_aligns[0].s1, current.e1 + 2):
                        if (ref in snps) and (sorted_aligns[0].contig in snps[ref]) and (z in snps[ref][sorted_aligns[0].contig]):
                            for cur_snp in snps[ref][sorted_aligns[0].contig][z]:
                                if cur_snp.type == 'I':
                                    snip_left += 1
                                elif cur_snp.type == 'D':
                                    snip_left -= 1
                    #Modifying position in contig of next alignment
                    sorted_aligns[0].s1 = current.e1 + 1
                    if sorted_aligns[0].s2 < sorted_aligns[0].e2:
                        print >> planta_out_f, '\t\t\t\tMoving contig start from %d to %d.' % (sorted_aligns[0].s2, sorted_aligns[0].s2 + snip_left)
                        sorted_aligns[0].s2 += snip_left
                    else:
                        print >> planta_out_f, '\t\t\t\tMoving contig start from %d to %d.' % (sorted_aligns[0].s2, sorted_aligns[0].s2 - snip_left)
                        sorted_aligns[0].s2 -= snip_left
                    negative = False

                #Pull top alignment
                current = sorted_aligns[0]
                sorted_aligns = sorted_aligns[1:]
                #print >>plantafile_out, '\t\t\tAlign %d: %s' % (counter, current)  #(self, s1, e1, s2, e2, len1, len2, idy, ref, contig):
                print >>planta_out_f, '\t\t\tAlign %d: %s' % (counter, '%d %d %s %d %d' % (current.s1, current.e1, current.contig, current.s2, current.e2))

                #Check if:
                # A) We have no more aligns to this reference
                # B) The current alignment extends to or past the end of the region
                # C) The next alignment starts after the end of the region

                if not sorted_aligns or current.e1 >= region[1] or sorted_aligns[0].s1 > region[1]:
                    #Check if last alignment ends before the regions does (gap at end of the region)
                    if current.e1 >= region[1]:
                        #print "Ends inside current alignment.\n";
                        print >> planta_out_f, '\t\t\tEND in current alignment.  Modifying %d to %d.' % (current.e1, region[1])
                        #Pushing the rest of the alignment back on the stack
                        sorted_aligns = [current] + sorted_aligns
                        #Flag to end loop through alignment
                        end = True
                        #Clip off right side of contig alignment
                        snip_right = current.e1 - region[1]
                        #End current alignment in region
                        current.e1 = region[1]
                    else:
                        #Region ends in a gap
                        size = region[1] - current.e1
                        print >> planta_out_f, '\t\t\tEND in gap: %d to %d (%d bp)' % (current.e1, region[1], size)

                        #Record gap
                        if not sorted_aligns:
                            #No more alignments, region ends in gap.
                            gaps.append([size, current.contig, 'END'])
                        else:
                            #Gap between end of current and beginning of next alignment.
                            gaps.append([size, current.contig, sorted_aligns[0].contig])
                        #Increment any ambiguous bases within this gap
                        for i in xrange(current.e1, region[1]):
                            if (ref in ref_features) and (i in ref_features[ref]) and (ref_features[ref][i] == 'A'):
                                region_ambig += 1
                else:
                    #Grab next alignment
                    next = sorted_aligns[0]
                    #print >> plantafile_out, '\t\t\t\tNext Alignment: %d %d %s %d %d' % (next.s1, next.e1, next.contig, next.s2, next.e2)

                    if next.e1 <= current.e1:
                        #The next alignment is redundant to the current alignmentt
                        while next.e1 <= current.e1 and sorted_aligns:
                            total_redundant += next.e1 - next.s1 + 1
                            print >> planta_out_f, '\t\t\t\tThe next alignment (%d %d %s %d %d) is redundant. Skipping.' \
                                                     % (next.s1, next.e1, next.contig, next.s2, next.e2)
                            redundant.append(current.contig)
                            sorted_aligns = sorted_aligns[1:]
                            if sorted_aligns:
                                next = sorted_aligns[0]
                                counter += 1
                            else:
                                #Flag to end loop through alignment
                                end = True

                    if not end:
                        if next.s1 > current.e1 + 1:
                            #There is a gap beetween this and the next alignment
                            size = next.s1 - current.e1 - 1
                            gaps.append([size, current.contig, next.contig])
                            print >> planta_out_f, '\t\t\t\tGap between this and next alignment: %d to %d (%d bp)' % (current.e1, next.s1, size)
                            #Record ambiguous bases in current gap
                            for i in xrange(current.e1, next.s1):
                                if (ref in ref_features) and (i in ref_features[ref]) and (ref_features[ref][i] == 'A'):
                                    region_ambig += 1
                        elif next.s1 <= current.e1:
                            #This alignment overlaps with the next alignment, negative gap
                            #If contig extends past the region, clip
                            if current.e1 > region[1]:
                                current.e1 = region[1]
                            #Record gap
                            size = next.s1 - current.e1
                            neg_gaps.append([size, current.contig, next.contig])
                            print >>planta_out_f, '\t\t\t\tNegative gap (overlap) between this and next alignment: %d to %d (%d bp)' % (current.e1, next.s1, size)

                            #Mark this alignment as negative so overlap region can be ignored
                            negative = True
                        print >> planta_out_f, '\t\t\t\tNext Alignment: %d %d %s %d %d' % (next.s1, next.e1, next.contig, next.s2, next.e2)

                #Initiate location of SNP on assembly to be first or last base of contig alignment
                contig_estimate = current.s2
                enable_SNPs_output = False
                if enable_SNPs_output:
                    print >> planta_out_f, '\t\t\t\tContig start coord: %d' % contig_estimate

                #Assess each reference base of the current alignment
                for i in xrange(current.s1, current.e1 + 1):
                    #Mark as covered
                    region_covered += 1

                    if current.s2 < current.e2:
                        pos_strand = True
                    else:
                        pos_strand = False

                    #If there is a misassembly, increment count and contig length
                    #if (exists $ref_features{$ref}[$i] && $ref_features{$ref}[$i] eq "M") {
                    #	$region_misassemblies++;
                    #	$misassembled_contigs{$current[2]} = length($assembly{$current[2]});
                    #}

                    #If there is a SNP, and no alternative alignments over this base, record SNPs
                    if (ref in snps) and (current.contig in snps[ref]) and (i in snps[ref][current.contig]):
                        cur_snps = snps[ref][current.contig][i]
                        # sorting by pos in contig
                        if pos_strand:
                            cur_snps = sorted(cur_snps, key=lambda x: x.ctg_pos)
                        else: # for reverse complement
                            cur_snps = sorted(cur_snps, key=lambda x: x.ctg_pos, reverse=True)

                        for cur_snp in cur_snps:
                            if enable_SNPs_output:
                                print >> planta_out_f, '\t\t\t\tSNP: %s, reference coord: %d, contig coord: %d, estimated contig coord: %d' % \
                                         (cur_snp.type, i, cur_snp.ctg_pos, contig_estimate)

                            #Capture SNP base
                            snp = cur_snp.type

                            #Check that there are not multiple alignments at this location
                            ### Alex: obsolete, we changed algorithm for ambiguous contigs
                            #if (ref in ref_features) and (i in ref_features[ref]):
                            #    print >> plantafile_out, '\t\t\t\t\tERROR: SNP at a position where there are multiple alignments (%s).  Skipping.\n' % ref_features[ref][i]
                            #    if current.s2 < current.e2: contig_estimate += 1
                            #    else: contig_estimate -= 1
                            #    continue
                            #Check that the position of the SNP in the contig is close to the position of this SNP
                            if abs(contig_estimate - cur_snp.ctg_pos) > 2:
                                if enable_SNPs_output:
                                    print >> planta_out_f, '\t\t\t\t\tERROR: SNP position in contig was off by %d bp! (%d vs %d)' \
                                             % (abs(contig_estimate - cur_snp.ctg_pos), contig_estimate, cur_snp.ctg_pos)
                                continue

                            print >> used_snps_file, '%s\t%s\t%d\t%s\t%s\t%d' % (ref, current.contig, cur_snp.ref_pos,
                                                                                 cur_snp.ref_nucl, cur_snp.ctg_nucl, cur_snp.ctg_pos)

                            #If SNP is an insertion, record
                            if snp == 'I':
                                total_indels_info.insertions += 1
                                if pos_strand: contig_estimate += 1
                                else: contig_estimate -= 1
                            #If SNP is a deletion, record
                            if snp == 'D':
                                total_indels_info.deletions += 1
                                if pos_strand: contig_estimate -= 1
                                else: contig_estimate += 1
                            #If SNP is a mismatch, record
                            if snp == 'S':
                                total_indels_info.mismatches += 1

                            if cur_snp.type == 'D' or cur_snp.type == 'I':
                                if prev_snp and ((cur_snp.type == 'D' and (prev_snp.ref_pos == cur_snp.ref_pos - 1) and (prev_snp.ctg_pos == cur_snp.ctg_pos)) or
                                     (cur_snp.type == 'I' and ((pos_strand and (prev_snp.ctg_pos == cur_snp.ctg_pos - 1)) or
                                         (not pos_strand and (prev_snp.ctg_pos == cur_snp.ctg_pos + 1))) and (prev_snp.ref_pos == cur_snp.ref_pos))):
                                    cur_indel += 1
                                else:
                                    if cur_indel:
                                        total_indels_info.indels_list.append(cur_indel)
                                    cur_indel = 1
                                prev_snp = cur_snp

                    if pos_strand: contig_estimate += 1
                    else: contig_estimate -= 1

                #Record Ns in current alignment
                if current.s2 < current.e2:
                    #print "\t\t(forward)Recording Ns from $current[3]+$snip_left to $current[4]-$snip_right...\n";
                    for i in (current.s2 + snip_left, current.e2 - snip_right + 1):
                        if contig_ns and (i in contig_ns):
                            region_ambig += 1
                else:
                    #print "\t\t(reverse)Recording Ns from $current[4]+$snip_right to $current[3]-$snip_left...\n";
                    for i in (current.e2 + snip_left, current.s2 - snip_right + 1):
                        if contig_ns and (i in contig_ns):
                            region_ambig += 1
                snip_left = 0
                snip_right = 0

                if cur_indel:
                    total_indels_info.indels_list.append(cur_indel)
                prev_snp = None
                cur_indel = 0

                print >> planta_out_f

    ##### getting results from Plantagora's algorithm
    SNPs = total_indels_info.mismatches
    indels_list = total_indels_info.indels_list
    total_aligned_bases = region_covered
    print >> planta_out_f, 'Analysis is finished!'
    if qconfig.show_snps:
        print >> planta_out_f, 'Founded SNPs were written into', used_snps_fpath
    print >> planta_out_f, '\nResults:'

    print >> planta_out_f, '\tLocal Misassemblies: %d' % region_misassemblies.count(Misassembly.LOCAL)
    print >> planta_out_f, '\tMisassemblies: %d' % (len(region_misassemblies) - region_misassemblies.count(Misassembly.LOCAL)
                                                    - region_misassemblies.count(Misassembly.SCAFFOLD_GAP) - region_misassemblies.count(Misassembly.FRAGMENTED))
    print >> planta_out_f, '\t\tRelocations: %d' % region_misassemblies.count(Misassembly.RELOCATION)
    print >> planta_out_f, '\t\tTranslocations: %d' % region_misassemblies.count(Misassembly.TRANSLOCATION)
    if qconfig.is_combined_ref:
        print >> planta_out_f, '\t\tInterspecies translocations: %d' % region_misassemblies.count(Misassembly.INTERSPECTRANSLOCATION)
    print >> planta_out_f, '\t\tInversions: %d' % region_misassemblies.count(Misassembly.INVERSION)
    if qconfig.meta:
        print >> planta_out_f, '\tPotentially Misassembled Contigs (i/s translocations): %d' % region_misassemblies.count(Misassembly.POTENTIALLY_MIS_CONTIGS)
        print >> planta_out_f, '\tPotential Misassemblies (i/s translocations): %d' % region_misassemblies.count(Misassembly.POTENTIALLY_MISASSEMBLIES)
    if qconfig.scaffolds and contigs_fpath not in qconfig.dict_of_broken_scaffolds:
        print >> planta_out_f, '\tScaffold gap misassemblies: %d' % region_misassemblies.count(Misassembly.SCAFFOLD_GAP)
    if bed_fpath:
        print >> planta_out_f, '\tFake misassemblies matched with structural variations: %d' % misassemblies_matched_sv

    if qconfig.check_for_fragmented_ref:
        print >> planta_out_f, '\tMisassemblies caused by fragmented reference: %d' % region_misassemblies.count(Misassembly.FRAGMENTED)
    print >> planta_out_f, '\tMisassembled Contigs: %d' % len(misassembled_contigs)
    misassembled_bases = sum(misassembled_contigs.itervalues())
    print >> planta_out_f, '\tMisassembled Contig Bases: %d' % misassembled_bases
    print >> planta_out_f, '\tMisassemblies Inter-Contig Overlap: %d' % misassembly_internal_overlap
    print >> planta_out_f, 'Uncovered Regions: %d (%d)' % (uncovered_regions, uncovered_region_bases)
    print >> planta_out_f, 'Unaligned Contigs: %d + %d part' % (unaligned, partially_unaligned)
    print >> planta_out_f, 'Partially Unaligned Contigs with Misassemblies: %d' % partially_unaligned_with_misassembly
    print >> planta_out_f, 'Unaligned Contig Bases: %d' % (fully_unaligned_bases + partially_unaligned_bases)

    print >> planta_out_f, ''
    print >> planta_out_f, 'Ambiguously Mapped Contigs: %d' % ambiguous_contigs
    if qconfig.ambiguity_usage == "all":
        print >> planta_out_f, 'Extra Bases in Ambiguously Mapped Contigs: %d' % ambiguous_contigs_extra_bases
        print >> planta_out_f, 'Note that --allow-ambiguity option was set to "all" and each of these contigs was used several times.'
    else:
        print >> planta_out_f, 'Total Bases in Ambiguously Mapped Contigs: %d' % (-ambiguous_contigs_extra_bases)
        if qconfig.ambiguity_usage == "none":
            print >> planta_out_f, 'Note that --allow-ambiguity option was set to "none" and these contigs were skipped.'
        else:
            print >> planta_out_f, 'Note that --allow-ambiguity option was set to "one" and only first alignment per each of these contigs was used.'
            ambiguous_contigs_extra_bases = 0 # this variable is used in Duplication ratio but we don't need it in this case

    if qconfig.show_snps:
        #print >> plantafile_out, 'Mismatches: %d' % SNPs
        #print >> plantafile_out, 'Single Nucleotide Indels: %d' % indels

        print >> planta_out_f, ''
        print >> planta_out_f, '\tCovered Bases: %d' % region_covered
        #print >> plantafile_out, '\tAmbiguous Bases (e.g. N\'s): %d' % region_ambig
        print >> planta_out_f, ''
        print >> planta_out_f, '\tSNPs: %d' % total_indels_info.mismatches
        print >> planta_out_f, '\tInsertions: %d' % total_indels_info.insertions
        print >> planta_out_f, '\tDeletions: %d' % total_indels_info.deletions
        #print >> plantafile_out, '\tList of indels lengths:', indels_list
        print >> planta_out_f, ''
        print >> planta_out_f, '\tPositive Gaps: %d' % len(gaps)
        internal = 0
        external = 0
        summ = 0
        for gap in gaps:
            if gap[1] == gap[2]:
                internal += 1
            else:
                external += 1
                summ += gap[0]
        print >> planta_out_f, '\t\tInternal Gaps: % d' % internal
        print >> planta_out_f, '\t\tExternal Gaps: % d' % external
        print >> planta_out_f, '\t\tExternal Gap Total: % d' % summ
        if external:
            avg = summ * 1.0 / external
        else:
            avg = 0.0
        print >> planta_out_f, '\t\tExternal Gap Average: %.0f' % avg

        print >> planta_out_f, '\tNegative Gaps: %d' % len(neg_gaps)
        internal = 0
        external = 0
        summ = 0
        for gap in neg_gaps:
            if gap[1] == gap[2]:
                internal += 1
            else:
                external += 1
                summ += gap[0]
        print >> planta_out_f, '\t\tInternal Overlaps: % d' % internal
        print >> planta_out_f, '\t\tExternal Overlaps: % d' % external
        print >> planta_out_f, '\t\tExternal Overlaps Total: % d' % summ
        if external:
            avg = summ * 1.0 / external
        else:
            avg = 0.0
        print >> planta_out_f, '\t\tExternal Overlaps Average: %.0f' % avg

        redundant = list(set(redundant))
        print >> planta_out_f, '\tContigs with Redundant Alignments: %d (%d)' % (len(redundant), total_redundant)

    if not qconfig.show_snps:
        SNPs = None
        indels_list = None
        total_aligned_bases = None

    result = {'avg_idy': avg_idy, 'region_misassemblies': region_misassemblies,
              'region_struct_variations': region_struct_variations.get_count() if region_struct_variations else None,
              'misassemblies_matched_sv': misassemblies_matched_sv,
              'misassembled_contigs': misassembled_contigs, 'misassembled_bases': misassembled_bases,
              'misassembly_internal_overlap': misassembly_internal_overlap,
              'unaligned': unaligned, 'partially_unaligned': partially_unaligned,
              'partially_unaligned_bases': partially_unaligned_bases, 'fully_unaligned_bases': fully_unaligned_bases,
              'ambiguous_contigs': ambiguous_contigs, 'ambiguous_contigs_extra_bases': ambiguous_contigs_extra_bases, 'SNPs': SNPs, 'indels_list': indels_list,
              'total_aligned_bases': total_aligned_bases,
              'partially_unaligned_with_misassembly': partially_unaligned_with_misassembly,
              'partially_unaligned_with_significant_parts': partially_unaligned_with_significant_parts,
              'istranslocations_by_refs': references_misassemblies,
              'total_misassemblies_by_refs': total_misassemblies_by_refs}

    ## outputting misassembled contigs to separate file
    fasta = [(name, seq) for name, seq in fastaparser.read_fasta(contigs_fpath)
             if name in misassembled_contigs.keys()]
    fastaparser.write_fasta(
        os.path.join(output_dirpath,
                     qutils.name_from_fpath(contigs_fpath) + '.mis_contigs.fa'),
        fasta)

    alignment_tsv_fpath = os.path.join(output_dirpath, "alignments_" + assembly_label + '.tsv')
    logger.debug('  ' + qutils.index_to_str(index) + 'Alignments: ' + qutils.relpath(alignment_tsv_fpath))
    alignment_tsv_f = open(alignment_tsv_fpath, 'w')
    for ref_name, aligns in ref_aligns.iteritems():
        alignment_tsv_f.write(ref_name)
        for align in aligns:
            alignment_tsv_f.write('\t' + align.contig)
        alignment_tsv_f.write('\n')
    alignment_tsv_f.close()

    planta_out_f.close()
    planta_err_f.close()
    if qconfig.show_snps:
        used_snps_file.close()
    logger.info('  ' + qutils.index_to_str(index) + 'Analysis is finished.')
    logger.debug('')
    if nothing_aligned:
        return NucmerStatus.NOT_ALIGNED, result, aligned_lengths
    else:
        return NucmerStatus.OK, result, aligned_lengths


def plantakolya_process(cyclic, nucmer_output_dirpath, contigs_fpath, i, output_dirpath, ref_fpath, bed_fpath, parallel_by_chr=False,
                        emem_threads=1):
    contigs_fpath, old_contigs_fpath = contigs_fpath
    assembly_label = qutils.label_from_fpath_for_fname(contigs_fpath)

    nucmer_fname = os.path.join(nucmer_output_dirpath, assembly_label)
    nucmer_is_ok, result, aligned_lengths = plantakolya(cyclic, i, contigs_fpath, nucmer_fname, output_dirpath, ref_fpath,
                                                        old_contigs_fpath, bed_fpath, parallel_by_chr=parallel_by_chr, emem_threads=emem_threads)

    clear_files(contigs_fpath, nucmer_fname)
    return nucmer_is_ok, result, aligned_lengths


def do(reference, contigs_fpaths, cyclic, output_dir, old_contigs_fpaths, bed_fpath=None):
    if not os.path.isdir(output_dir):
        os.mkdir(output_dir)

    logger.print_timestamp()
    logger.main_info('Running Contig analyzer...')
    num_nf_errors = logger._num_nf_errors

    if not ca_utils.compile_aligner(logger):
        logger.main_info('Failed aligning the contigs for all the assemblies. Only basic stats are going to be evaluated.')
        return dict(zip(contigs_fpaths, [NucmerStatus.FAILED] * len(contigs_fpaths))), None

    nucmer_output_dirname = 'nucmer_output'
    nucmer_output_dir = os.path.join(output_dir, nucmer_output_dirname)
    if not os.path.isdir(nucmer_output_dir):
        os.mkdir(nucmer_output_dir)
    if qconfig.is_combined_ref:
        from libs import search_references_meta
        if search_references_meta.is_quast_first_run:
            nucmer_output_dir = os.path.join(nucmer_output_dir, 'raw')
            if not os.path.isdir(nucmer_output_dir):
                os.mkdir(nucmer_output_dir)
    n_jobs = min(len(contigs_fpaths), qconfig.max_threads)
    emem_threads = max(int(qconfig.max_threads / len(contigs_fpaths)), 1)
    if qconfig.memory_efficient:
        emem_threads = 1
    from joblib import Parallel, delayed
    if not qconfig.splitted_ref:
        statuses_results_lengths_tuples = Parallel(n_jobs=n_jobs)(delayed(plantakolya_process)(
        cyclic, nucmer_output_dir, fname, i, output_dir, reference, bed_fpath, emem_threads=emem_threads)
             for i, fname in enumerate(zip(contigs_fpaths, old_contigs_fpaths)))
    else:
        if len(contigs_fpaths) >= len(qconfig.splitted_ref) and not qconfig.memory_efficient:
            statuses_results_lengths_tuples = Parallel(n_jobs=n_jobs)(delayed(plantakolya_process)(
        cyclic, nucmer_output_dir, fname, i, output_dir, reference, bed_fpath, emem_threads=emem_threads)
             for i, fname in enumerate(zip(contigs_fpaths, old_contigs_fpaths)))
        else:
            statuses_results_lengths_tuples = []
            for i, contigs_fpath in enumerate(zip(contigs_fpaths, old_contigs_fpaths)):
                statuses_results_lengths_tuples.append(plantakolya_process(cyclic, nucmer_output_dir, contigs_fpath, i,
                                                                           output_dir, reference, bed_fpath, parallel_by_chr=True))

    # unzipping
    statuses, results, aligned_lengths = [x[0] for x in statuses_results_lengths_tuples], \
                                         [x[1] for x in statuses_results_lengths_tuples], \
                                         [x[2] for x in statuses_results_lengths_tuples]
    reports = []

    def val_to_str(val):
        if val is None:
            return '-'
        else:
            return str(val)

    def print_file(all_rows, ref_num, fpath):
        colwidths = repeat(0)
        for row in all_rows:
            colwidths = [max(len(v), w) for v, w in zip([row['metricName']] + map(val_to_str, row['values']), colwidths)]
        txt_file = open(fpath, 'a')
        for row in all_rows:
            print >> txt_file, '  '.join('%-*s' % (colwidth, cell) for colwidth, cell
                                         in zip(colwidths, [row['metricName']] + map(val_to_str, row['values'])))

    if qconfig.is_combined_ref:
        import plotter

        ref_misassemblies = [result['istranslocations_by_refs'] if result else [] for result in results]
        total_misassemblies_by_refs = [result['total_misassemblies_by_refs'] if result else [] for result in results]
        added_refs = set()
        all_refs = [added_refs.add(ref) or ref for ref in ref_labels_by_chromosomes.values() if ref not in added_refs]
        for i, fpath in enumerate(contigs_fpaths):
            assembly_name = qutils.name_from_fpath(fpath)
            misassemblies_by_ref_plot_fpath = os.path.join(output_dir, 'all_misassemblies_%s' % assembly_name)
            if qconfig.draw_plots:
                plotter.draw_all_misassemblies_plot(total_misassemblies_by_refs[i], all_refs, misassemblies_by_ref_plot_fpath, assembly_name)
        if ref_misassemblies:
            labels = [qutils.label_from_fpath(contigs_fpath) for contigs_fpath in contigs_fpaths]
            plots_dirpath = os.path.join(output_dir, 'interspecies_translocations')
            if not os.path.exists(plots_dirpath):
                os.mkdir(plots_dirpath)
            for ref in all_refs:
                misassembly_by_ref_plot_fpath = os.path.join(plots_dirpath, '%s' % ref)
                if qconfig.draw_plots:
                    plotter.draw_interspecies_translocations_plot(ref_misassemblies, all_refs, ref, contigs_fpaths, misassembly_by_ref_plot_fpath)
            all_rows = []
            row = {'metricName': 'References', 'values': all_refs}
            all_rows.append(row)
            misassemblies = []
            for i, fpath in enumerate(contigs_fpaths):
                row = {'metricName': labels[i], 'values': []}
                all_rows.append(row)
                if ref_misassemblies[i]:
                    assembly_name = qutils.name_from_fpath(fpath)
                    cur_rows = []
                    row = {'metricName': 'References', 'values': [ref_num+1 for ref_num in range(len(all_refs))]}
                    cur_rows.append(row)
                    for k in all_refs:
                        row = {'metricName': k, 'values': []}
                        for ref in all_refs:
                            if ref == k or ref not in ref_misassemblies[i]:
                                row['values'].append(None)
                            else:
                                row['values'].append(ref_misassemblies[i][ref][k])
                        all_rows[-1]['values'].append(max(0, sum([r for r in row['values'] if r]) +
                                                          total_misassemblies_by_refs[0][k][Misassembly.POTENTIALLY_MISASSEMBLIES] ))
                        cur_rows.append(row)
                    misassembly_by_ref_fpath = os.path.join(output_dir, 'interspecies_translocations_by_refs_%s.info' % assembly_name)
                    print >> open(misassembly_by_ref_fpath, 'w'), 'Number of interspecies translocations by references: \n'
                    print_file(cur_rows, len(all_refs), misassembly_by_ref_fpath)

                    print >> open(misassembly_by_ref_fpath, 'a'), 'References: \n'
                    for ref_num, ref in enumerate(all_refs):
                        print >> open(misassembly_by_ref_fpath, 'a'), str(ref_num+1) + ' - ' + ref
                    logger.info('  Information about interspecies translocations by references for %s is saved to %s' %
                                (assembly_name, misassembly_by_ref_fpath))
            aligned_contigs_labels = []
            for row in all_rows[1:]:
                if row['values']:
                    aligned_contigs_labels.append(row['metricName'])
                else:
                    all_rows.remove(row)
            for i in range(len(all_refs)):
                cur_results = []
                for row in all_rows[1:]:
                    if row['values']:
                        cur_results.append(row['values'][i])
                misassemblies.append(cur_results)
            is_translocations_plot_fpath = os.path.join(output_dir, 'interspecies_translocations.' + qconfig.plot_extension)
            plotter.draw_meta_summary_plot("", output_dir, aligned_contigs_labels, all_refs, all_rows, misassemblies, is_translocations_plot_fpath,
                                           title='Intergenomic misassemblies', reverse=False, yaxis_title=None)

    def save_result(result):
        report = reporting.get(fname)

        avg_idy = result['avg_idy']
        region_misassemblies = result['region_misassemblies']
        region_struct_variations = result['region_struct_variations']
        misassemblies_matched_sv = result['misassemblies_matched_sv']
        misassembled_contigs = result['misassembled_contigs']
        misassembled_bases = result['misassembled_bases']
        misassembly_internal_overlap = result['misassembly_internal_overlap']
        unaligned = result['unaligned']
        partially_unaligned = result['partially_unaligned']
        partially_unaligned_bases = result['partially_unaligned_bases']
        fully_unaligned_bases = result['fully_unaligned_bases']
        ambiguous_contigs = result['ambiguous_contigs']
        ambiguous_contigs_extra_bases = result['ambiguous_contigs_extra_bases']
        SNPs = result['SNPs']
        indels_list = result['indels_list']
        total_aligned_bases = result['total_aligned_bases']
        partially_unaligned_with_misassembly = result['partially_unaligned_with_misassembly']
        partially_unaligned_with_significant_parts = result['partially_unaligned_with_significant_parts']

        report.add_field(reporting.Fields.AVGIDY, '%.3f' % avg_idy)
        report.add_field(reporting.Fields.MISLOCAL, region_misassemblies.count(Misassembly.LOCAL))
        report.add_field(reporting.Fields.MISASSEMBL, len(region_misassemblies) - region_misassemblies.count(Misassembly.LOCAL)
                         - region_misassemblies.count(Misassembly.SCAFFOLD_GAP) - region_misassemblies.count(Misassembly.FRAGMENTED))
        report.add_field(reporting.Fields.MISCONTIGS, len(misassembled_contigs))
        report.add_field(reporting.Fields.MISCONTIGSBASES, misassembled_bases)
        report.add_field(reporting.Fields.MISINTERNALOVERLAP, misassembly_internal_overlap)
        if bed_fpath:
            report.add_field(reporting.Fields.STRUCT_VARIATIONS, misassemblies_matched_sv)
        report.add_field(reporting.Fields.UNALIGNED, '%d + %d part' % (unaligned, partially_unaligned))
        report.add_field(reporting.Fields.UNALIGNEDBASES, (fully_unaligned_bases + partially_unaligned_bases))
        report.add_field(reporting.Fields.AMBIGUOUS, ambiguous_contigs)
        report.add_field(reporting.Fields.AMBIGUOUSEXTRABASES, ambiguous_contigs_extra_bases)
        report.add_field(reporting.Fields.MISMATCHES, SNPs)
        # different types of indels:
        if indels_list is not None:
            report.add_field(reporting.Fields.INDELS, len(indels_list))
            report.add_field(reporting.Fields.INDELSBASES, sum(indels_list))
            report.add_field(reporting.Fields.MIS_SHORT_INDELS, len([i for i in indels_list if i <= qconfig.SHORT_INDEL_THRESHOLD]))
            report.add_field(reporting.Fields.MIS_LONG_INDELS, len([i for i in indels_list if i > qconfig.SHORT_INDEL_THRESHOLD]))

        if total_aligned_bases:
            report.add_field(reporting.Fields.SUBSERROR, "%.2f" % (float(SNPs) * 100000.0 / float(total_aligned_bases)))
            report.add_field(reporting.Fields.INDELSERROR, "%.2f" % (float(report.get_field(reporting.Fields.INDELS))
                                                                     * 100000.0 / float(total_aligned_bases)))

        # for misassemblies report:
        report.add_field(reporting.Fields.MIS_ALL_EXTENSIVE, len(region_misassemblies) - region_misassemblies.count(Misassembly.LOCAL)
                         - region_misassemblies.count(Misassembly.SCAFFOLD_GAP) - region_misassemblies.count(Misassembly.FRAGMENTED))
        report.add_field(reporting.Fields.MIS_RELOCATION, region_misassemblies.count(Misassembly.RELOCATION))
        report.add_field(reporting.Fields.MIS_TRANSLOCATION, region_misassemblies.count(Misassembly.TRANSLOCATION))
        report.add_field(reporting.Fields.MIS_INVERTION, region_misassemblies.count(Misassembly.INVERSION))
        report.add_field(reporting.Fields.MIS_EXTENSIVE_CONTIGS, len(misassembled_contigs))
        report.add_field(reporting.Fields.MIS_EXTENSIVE_BASES, misassembled_bases)
        report.add_field(reporting.Fields.MIS_LOCAL, region_misassemblies.count(Misassembly.LOCAL))
        if qconfig.is_combined_ref:
            report.add_field(reporting.Fields.MIS_ISTRANSLOCATIONS, region_misassemblies.count(Misassembly.INTERSPECTRANSLOCATION))
        if qconfig.meta:
            report.add_field(reporting.Fields.POTENTIALLY_MIS_CONTIGS, region_misassemblies.count(Misassembly.POTENTIALLY_MIS_CONTIGS))
            report.add_field(reporting.Fields.POTENTIALLY_MISASSEMBLIES, region_misassemblies.count(Misassembly.POTENTIALLY_MISASSEMBLIES))
        if qconfig.scaffolds and fname not in qconfig.dict_of_broken_scaffolds:
            report.add_field(reporting.Fields.MIS_SCAFFOLDS_GAP, region_misassemblies.count(Misassembly.SCAFFOLD_GAP))
        if qconfig.check_for_fragmented_ref:
            report.add_field(reporting.Fields.MIS_FRAGMENTED, region_misassemblies.count(Misassembly.FRAGMENTED))

        # for unaligned report:
        report.add_field(reporting.Fields.UNALIGNED_FULL_CNTGS, unaligned)
        report.add_field(reporting.Fields.UNALIGNED_FULL_LENGTH, fully_unaligned_bases)
        report.add_field(reporting.Fields.UNALIGNED_PART_CNTGS, partially_unaligned)
        report.add_field(reporting.Fields.UNALIGNED_PART_WITH_MISASSEMBLY, partially_unaligned_with_misassembly)
        report.add_field(reporting.Fields.UNALIGNED_PART_SIGNIFICANT_PARTS, partially_unaligned_with_significant_parts)
        report.add_field(reporting.Fields.UNALIGNED_PART_LENGTH, partially_unaligned_bases)
        reports.append(report)

    def save_result_for_unaligned(result):
        report = reporting.get(fname)

        unaligned_ctgs = report.get_field(reporting.Fields.CONTIGS)
        unaligned_length = report.get_field(reporting.Fields.TOTALLEN)
        report.add_field(reporting.Fields.UNALIGNED, '%d + %d part' % (unaligned_ctgs, 0))
        report.add_field(reporting.Fields.UNALIGNEDBASES, unaligned_length)

        report.add_field(reporting.Fields.UNALIGNED_FULL_CNTGS, unaligned_ctgs)
        report.add_field(reporting.Fields.UNALIGNED_FULL_LENGTH, unaligned_length)

    for index, fname in enumerate(contigs_fpaths):
        if statuses[index] == NucmerStatus.OK:
            save_result(results[index])
        elif statuses[index] == NucmerStatus.NOT_ALIGNED:
            save_result_for_unaligned(results[index])

    nucmer_statuses = dict(zip(contigs_fpaths, statuses))
    aligned_lengths_per_fpath = dict(zip(contigs_fpaths, aligned_lengths))

#    nucmer_statuses = {}
#
#    for id, filename in enumerate(filenames):
#        nucmer_status = plantakolya_process(cyclic, filename, id, myenv, output_dir, reference)
#        nucmer_statuses[filename] = nucmer_status

    if NucmerStatus.OK in nucmer_statuses.values():
        reporting.save_misassemblies(output_dir)
        reporting.save_unaligned(output_dir)
    if qconfig.draw_plots:
        import plotter
        plotter.draw_misassembl_plot(reports, output_dir + '/misassemblies_plot', 'Misassemblies')

    oks = nucmer_statuses.values().count(NucmerStatus.OK)
    not_aligned = nucmer_statuses.values().count(NucmerStatus.NOT_ALIGNED)
    failed = nucmer_statuses.values().count(NucmerStatus.FAILED)
    errors = nucmer_statuses.values().count(NucmerStatus.ERROR)
    problems = not_aligned + failed + errors
    all = len(nucmer_statuses)

    logger._num_nf_errors = num_nf_errors + errors

    if oks == all:
        logger.main_info('Done.')
    if oks < all and problems < all:
        logger.main_info('Done for ' + str(all - problems) + ' out of ' + str(all) + '. For the rest, only basic stats are going to be evaluated.')
    if problems == all:
        logger.main_info('Failed aligning the contigs for all the assemblies. Only basic stats are going to be evaluated.')

#    if NucmerStatus.FAILED in nucmer_statuses.values():
#        log.info('  ' + str(failed) + 'file' + (' ' if failed == 1 else 's ') + 'failed to align to the reference. Only basic stats have been evaluated.')
#    if NucmerStatus.NOT_ALIGNED in nucmer_statuses.values():
#        log.info('  ' + str(not_aligned) + ' file' + (' was' if not_aligned == 1 else 's were') + ' not aligned to the reference. Only basic stats have been evaluated.')
#    if problems == all:
#        log.info('  Nucmer failed.')

    return nucmer_statuses, aligned_lengths_per_fpath
