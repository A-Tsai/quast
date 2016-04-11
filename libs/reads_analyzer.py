############################################################################
# Copyright (c) 2015-2016 Saint Petersburg State University
# Copyright (c) 2011-2015 Saint Petersburg Academic University
# All Rights Reserved
# See file LICENSE for details.
############################################################################

from __future__ import with_statement
import shutil
import urllib
import urllib2
from libs import reporting, qconfig, qutils, contigs_analyzer
from qutils import is_non_empty_file

from libs.log import get_logger

logger = get_logger(qconfig.LOGGER_DEFAULT_NAME)
import shlex
import os

bowtie_dirpath = os.path.join(qconfig.LIBS_LOCATION, 'bowtie2')
samtools_dirpath = os.path.join(qconfig.LIBS_LOCATION, 'samtools')
manta_dirpath = os.path.join(qconfig.LIBS_LOCATION, 'manta')
manta_build_dirpath = os.path.join(qconfig.LIBS_LOCATION, 'manta', 'build')
manta_bin_dirpath = os.path.join(qconfig.LIBS_LOCATION, 'manta', 'build/bin')
config_manta_fpath = os.path.join(manta_bin_dirpath, 'configManta.py')
manta_download_path = 'https://github.com/Illumina/manta/releases/download/v0.29.1/manta-0.29.1.centos5_x86_64.tar.bz2'


class Mapping(object):
    MIN_MAP_QUALITY = 20  # for distiguishing "good" reads and "bad" ones

    def __init__(self, fields):
        self.ref, self.start, self.mapq, self.ref_next, self.len = \
            fields[2], int(fields[3]), int(fields[4]), fields[6], len(fields[9])
        self.end = self.start + self.len - 1  # actually not always true because of indels

    @staticmethod
    def parse(line):
        if line.startswith('@'):  # comment
            return None
        if line.split('\t') < 11:  # not valid line
            return None
        mapping = Mapping(line.split('\t'))
        return mapping


class QuastDeletion(object):
    ''' describes situtations: GGGGBBBBBNNNNNNNNNNNNBBBBBBGGGGGG, where
    G -- "good" read (high mapping quality)
    B -- "bad" read (low mapping quality)
    N -- no mapped reads
    size of Ns fragment -- "deletion" (not less than MIN_GAP)
    size of Bs fragment -- confidence interval (not more than MAX_CONFIDENCE_INTERVAL,
        fixing last/first G position otherwise)
    '''

    MAX_CONFIDENCE_INTERVAL = 150
    MIN_GAP = qconfig.extensive_misassembly_threshold - 2 * MAX_CONFIDENCE_INTERVAL

    def __init__(self, ref, prev_good=None, prev_bad=None, next_bad=None, next_good=None, next_bad_end=None):
        self.ref, self.prev_good, self.prev_bad, self.next_bad, self.next_good, self.next_bad_end = \
            ref, prev_good, prev_bad, next_bad, next_good, next_bad_end
        self.id = 'QuastDEL'

    def is_valid(self):
        return self.prev_good is not None and self.prev_bad is not None and \
               self.next_bad is not None and self.next_good is not None and \
               (self.next_bad - self.prev_bad > QuastDeletion.MIN_GAP)

    def set_prev_good(self, mapping):
        self.prev_good = mapping.end
        self.prev_bad = self.prev_good  # prev_bad cannot be earlier than prev_good!
        return self  # to use this function like "deletion = QuastDeletion(ref).set_prev_good(coord)"

    def set_prev_bad(self, mapping=None, position=None):
        self.prev_bad = position if position else mapping.end
        if self.prev_good is None or self.prev_good + QuastDeletion.MAX_CONFIDENCE_INTERVAL < self.prev_bad:
            self.prev_good = max(1, self.prev_bad - QuastDeletion.MAX_CONFIDENCE_INTERVAL)
        return self  # to use this function like "deletion = QuastDeletion(ref).set_prev_bad(coord)"

    def set_next_good(self, mapping=None, position=None):
        self.next_good = position if position else mapping.start
        if self.next_bad is None:
            self.next_bad = self.next_good
        elif self.next_good - QuastDeletion.MAX_CONFIDENCE_INTERVAL > self.next_bad:
            self.next_good = self.next_bad + QuastDeletion.MAX_CONFIDENCE_INTERVAL

    def set_next_bad(self, mapping):
        self.next_bad = mapping.start
        self.next_bad_end = mapping.end
        self.next_good = self.next_bad  # next_good is always None at this moment (deletion is complete otherwise)

    def set_next_bad_end(self, mapping):
        if self.next_bad is None:
            self.next_bad = mapping.start
        self.next_bad_end = mapping.end
        self.next_good = min(mapping.start, self.next_bad + QuastDeletion.MAX_CONFIDENCE_INTERVAL)

    def __str__(self):
        return '\t'.join(map(str, [self.ref, self.prev_good, self.prev_bad,
                          self.ref, self.next_bad, self.next_good,
                          self.id]) + ['-'] * 4)


def process_one_ref(cur_ref_fpath, output_dirpath, err_path, bed_fpath=None):
    ref = qutils.name_from_fpath(cur_ref_fpath)
    ref_sam_fpath = os.path.join(output_dirpath, ref + '.sam')
    ref_bam_fpath = os.path.join(output_dirpath, ref + '.bam')
    ref_bamsorted_fpath = os.path.join(output_dirpath, ref + '.sorted')
    ref_bed_fpath = bed_fpath if bed_fpath else os.path.join(output_dirpath, ref + '.bed')
    if os.path.getsize(ref_sam_fpath) < 1024 * 1024:  # TODO: make it better (small files will cause Manta crush -- "not enough reads...")
        logger.info('  SAM file is too small for Manta (%d Kb), skipping..' % (os.path.getsize(ref_sam_fpath) // 1024))
        return None
    if is_non_empty_file(ref_bed_fpath):
        logger.info('  Using existing Manta BED-file: ' + ref_bed_fpath)
        return ref_bed_fpath
    if not os.path.exists(ref_bamsorted_fpath + '.bam'):
        qutils.call_subprocess([samtools_fpath('samtools'), 'view', '-bS', ref_sam_fpath], stdout=open(ref_bam_fpath, 'w'),
                               stderr=open(err_path, 'a'), logger=logger)
        qutils.call_subprocess([samtools_fpath('samtools'), 'sort', ref_bam_fpath, ref_bamsorted_fpath],
                               stderr=open(err_path, 'a'), logger=logger)
    if not is_non_empty_file(ref_bamsorted_fpath + '.bam.bai'):
        qutils.call_subprocess([samtools_fpath('samtools'), 'index', ref_bamsorted_fpath + '.bam'],
                               stderr=open(err_path, 'a'), logger=logger)
    if not is_non_empty_file(cur_ref_fpath + '.fai'):
        qutils.call_subprocess([samtools_fpath('samtools'), 'faidx', cur_ref_fpath],
                               stderr=open(err_path, 'a'), logger=logger)
    vcfoutput_dirpath = os.path.join(output_dirpath, ref + '_manta')
    found_SV_fpath = os.path.join(vcfoutput_dirpath, 'results/variants/diploidSV.vcf.gz')
    unpacked_SV_fpath = found_SV_fpath + '.unpacked'
    if not is_non_empty_file(found_SV_fpath):
        if os.path.exists(vcfoutput_dirpath):
            shutil.rmtree(vcfoutput_dirpath, ignore_errors=True)
        os.makedirs(vcfoutput_dirpath)
        qutils.call_subprocess([config_manta_fpath, '--normalBam', ref_bamsorted_fpath + '.bam',
                                '--referenceFasta', cur_ref_fpath, '--runDir', vcfoutput_dirpath],
                               stdout=open(err_path, 'a'), stderr=open(err_path, 'a'), logger=logger)
        if not os.path.exists(os.path.join(vcfoutput_dirpath, 'runWorkflow.py')):
            return None
        qutils.call_subprocess([os.path.join(vcfoutput_dirpath, 'runWorkflow.py'), '-m', 'local', '-j', str(qconfig.max_threads)],
                               stderr=open(err_path, 'a'), logger=logger)
    if not is_non_empty_file(unpacked_SV_fpath):
        cmd = 'gunzip -c %s' % found_SV_fpath
        qutils.call_subprocess(shlex.split(cmd), stdout=open(unpacked_SV_fpath, 'w'),
                               stderr=open(err_path, 'a'), logger=logger)
    from manta import vcfToBedpe
    vcfToBedpe.vcfToBedpe(open(unpacked_SV_fpath), open(ref_bed_fpath, 'w'))
    return ref_bed_fpath


def search_sv_with_manta(main_ref_fpath, meta_ref_fpaths, output_dirpath, err_path):
    logger.info('  Searching structural variations with Manta...')
    final_bed_fpath = os.path.join(output_dirpath, qconfig.manta_sv_fname)
    if os.path.exists(final_bed_fpath):
        logger.info('    Using existing file: ' + final_bed_fpath)
        return final_bed_fpath

    if meta_ref_fpaths:
        from joblib import Parallel, delayed
        n_jobs = min(len(meta_ref_fpaths), qconfig.max_threads)
        bed_fpaths = Parallel(n_jobs=n_jobs)(delayed(process_one_ref)(cur_ref_fpath, output_dirpath, err_path) for cur_ref_fpath in meta_ref_fpaths)
        bed_fpaths = [f for f in bed_fpaths if f is not None]
        if bed_fpaths:
            qutils.cat_files(bed_fpaths, final_bed_fpath)
    else:
        process_one_ref(main_ref_fpath, output_dirpath, err_path, bed_fpath=final_bed_fpath)
    logger.info('    Saving to: ' + final_bed_fpath)
    return final_bed_fpath


def run_processing_reads(main_ref_fpath, meta_ref_fpaths, ref_labels, reads_fpaths, output_dirpath, res_path, log_path,
                         err_path, bed_fpath=None):
    ref_name = qutils.name_from_fpath(main_ref_fpath)
    sam_fpath = os.path.join(output_dirpath, ref_name + '.sam')
    bam_fpath = os.path.join(output_dirpath, ref_name + '.bam')
    bam_sorted_fpath = os.path.join(output_dirpath, ref_name + '.sorted')
    sam_sorted_fpath = os.path.join(output_dirpath, ref_name + '.sorted.sam')
    bed_fpath = bed_fpath or os.path.join(res_path, ref_name + '.bed')
    cov_fpath = os.path.join(res_path, ref_name + '.cov')

    if os.path.exists(bed_fpath):
        logger.info('  Using existing BED-file: ' + bed_fpath)
        if not os.path.isfile(cov_fpath):
            cov_fpath = get_coverage(output_dirpath, ref_name, bam_fpath, err_path, cov_fpath)
        return bed_fpath, cov_fpath

    logger.info('  ' + 'Pre-processing for searching structural variations...')
    logger.info('  ' + 'Logging to %s...' % err_path)
    if is_non_empty_file(sam_fpath):
        logger.info('  Using existing SAM-file: ' + sam_fpath)
    else:
        logger.info('  Running Bowtie2...')
        abs_reads_fpaths = []  # use absolute paths because we will change workdir
        for reads_fpath in reads_fpaths:
            abs_reads_fpaths.append(os.path.abspath(reads_fpath))

        prev_dir = os.getcwd()
        os.chdir(output_dirpath)
        cmd = [bin_fpath('bowtie2-build'), main_ref_fpath, ref_name]
        qutils.call_subprocess(cmd, stdout=open(log_path, 'a'), stderr=open(err_path, 'a'), logger=logger)

        cmd = bin_fpath('bowtie2') + ' -x ' + ref_name + ' -1 ' + abs_reads_fpaths[0] + ' -2 ' + abs_reads_fpaths[1] + ' -S ' + \
              sam_fpath + ' --no-unal -a -p %s' % str(qconfig.max_threads)
        qutils.call_subprocess(shlex.split(cmd), stdout=open(log_path, 'a'), stderr=open(err_path, 'a'), logger=logger)
        logger.info('  Done.')
        os.chdir(prev_dir)
        if not os.path.exists(sam_fpath) or os.path.getsize(sam_fpath) == 0:
            logger.error('  Failed running Bowtie2 for the reference. See ' + log_path + ' for information.')
            logger.info('  Failed searching structural variations.')
            return None, None
    logger.info('  Sorting SAM-file...')
    if is_non_empty_file(sam_sorted_fpath):
        logger.info('  Using existing sorted SAM-file: ' + sam_sorted_fpath)
    else:
        qutils.call_subprocess([samtools_fpath('samtools'), 'view', '-@', str(qconfig.max_threads), '-bS', sam_fpath], stdout=open(bam_fpath, 'w'),
                               stderr=open(err_path, 'a'), logger=logger)
        qutils.call_subprocess([samtools_fpath('samtools'), 'sort', '-@', str(qconfig.max_threads), bam_fpath, bam_sorted_fpath],
                               stderr=open(err_path, 'a'), logger=logger)
        qutils.call_subprocess([samtools_fpath('samtools'), 'view', '-@', str(qconfig.max_threads), bam_sorted_fpath + '.bam'], stdout=open(sam_sorted_fpath, 'w'),
                               stderr=open(err_path, 'a'), logger=logger)

    cov_fpath = get_coverage(output_dirpath, ref_name, bam_fpath, err_path, cov_fpath)
    if meta_ref_fpaths:
        logger.info('  Splitting SAM-file by references...')
    headers = []
    seq_name_length = {}
    with open(sam_fpath) as sam_file:
        for line in sam_file:
            if not line.startswith('@'):
                break
            if line.startswith('@SQ') and 'SN:' in line and 'LN:' in line:
                seq_name = line.split('\tSN:')[1].split('\t')[0]
                seq_length = int(line.split('\tLN:')[1].split('\t')[0])
                seq_name_length[seq_name] = seq_length
            headers.append(line.strip())
    need_ref_splitting = False
    if meta_ref_fpaths:
        ref_files = {}
        for cur_ref_fpath in meta_ref_fpaths:
            ref = qutils.name_from_fpath(cur_ref_fpath)
            new_ref_sam_fpath = os.path.join(output_dirpath, ref + '.sam')
            if is_non_empty_file(new_ref_sam_fpath):
                logger.info('    Using existing split SAM-file for %s: %s' % (ref, new_ref_sam_fpath))
                ref_files[ref] = None
            else:
                new_ref_sam_file = open(new_ref_sam_fpath, 'w')
                new_ref_sam_file.write(headers[0] + '\n')
                chrs = []
                for h in (h for h in headers if h.startswith('@SQ') and 'SN:' in h):
                    seq_name = h.split('\tSN:')[1].split('\t')[0]
                    if seq_name in ref_labels and ref_labels[seq_name] == ref:
                        new_ref_sam_file.write(h + '\n')
                        chrs.append(seq_name)
                new_ref_sam_file.write(headers[-1] + '\n')
                ref_files[ref] = new_ref_sam_file
                need_ref_splitting = True
    deletions = []
    trivial_deletions_fpath = os.path.join(output_dirpath, qconfig.trivial_deletions_fname)
    logger.info('  Looking for trivial deletions (long zero-covered fragments)...')
    need_trivial_deletions = True
    if os.path.exists(trivial_deletions_fpath):
        need_trivial_deletions = False
        logger.info('    Using existing file: ' + trivial_deletions_fpath)

    if need_trivial_deletions or need_ref_splitting:
        with open(sam_sorted_fpath) as sam_file:
            cur_deletion = None
            for line in sam_file:
                mapping = Mapping.parse(line)
                if mapping:
                    # common case: continue current deletion (potential) on the same reference
                    if cur_deletion and cur_deletion.ref == mapping.ref:
                        if cur_deletion.next_bad is None:  # previous mapping was in region BEFORE 0-covered fragment
                            # just passed 0-covered fragment
                            if mapping.start - cur_deletion.prev_bad > QuastDeletion.MIN_GAP:
                                cur_deletion.set_next_bad(mapping)
                                if mapping.mapq >= Mapping.MIN_MAP_QUALITY:
                                    cur_deletion.set_next_good(mapping)
                                    if cur_deletion.is_valid():
                                        deletions.append(cur_deletion)
                                    cur_deletion = QuastDeletion(mapping.ref).set_prev_good(mapping)
                            # continue region BEFORE 0-covered fragment
                            elif mapping.mapq >= Mapping.MIN_MAP_QUALITY:
                                cur_deletion.set_prev_good(mapping)
                            else:
                                cur_deletion.set_prev_bad(mapping)
                        else:  # previous mapping was in region AFTER 0-covered fragment
                            # just passed another 0-cov fragment between end of cur_deletion BAD region and this mapping
                            if mapping.start - cur_deletion.next_bad_end > QuastDeletion.MIN_GAP:
                                if cur_deletion.is_valid():   # add previous fragment's deletion if needed
                                    deletions.append(cur_deletion)
                                cur_deletion = QuastDeletion(mapping.ref).set_prev_bad(position=cur_deletion.next_bad_end)
                            # continue region AFTER 0-covered fragment (old one or new/another one -- see "if" above)
                            if mapping.mapq >= Mapping.MIN_MAP_QUALITY:
                                cur_deletion.set_next_good(mapping)
                                if cur_deletion.is_valid():
                                    deletions.append(cur_deletion)
                                cur_deletion = QuastDeletion(mapping.ref).set_prev_good(mapping)
                            else:
                                cur_deletion.set_next_bad_end(mapping)
                    # special case: just started or just switched to the next reference
                    else:
                        if cur_deletion and cur_deletion.ref in seq_name_length:  # switched to the next ref
                            cur_deletion.set_next_good(position=seq_name_length[cur_deletion.ref])
                            if cur_deletion.is_valid():
                                deletions.append(cur_deletion)
                        cur_deletion = QuastDeletion(mapping.ref).set_prev_good(mapping)

                    if need_ref_splitting:
                        cur_ref = ref_labels[mapping.ref]
                        if mapping.ref_next.strip() == '=' or cur_ref == ref_labels[mapping.ref_next]:
                            if ref_files[cur_ref] is not None:
                                ref_files[cur_ref].write(line)
            if cur_deletion and cur_deletion.ref in seq_name_length:  # switched to the next ref
                cur_deletion.set_next_good(position=seq_name_length[cur_deletion.ref])
                if cur_deletion.is_valid():
                    deletions.append(cur_deletion)
        if need_ref_splitting:
            for ref_handler in ref_files.values():
                if ref_handler is not None:
                    ref_handler.close()
        if need_trivial_deletions:
            logger.info('  Trivial deletions: %d found' % len(deletions))
            logger.info('    Saving to: ' + trivial_deletions_fpath)
            with open(trivial_deletions_fpath, 'w') as f:
                for deletion in deletions:
                    f.write(str(deletion) + '\n')

    if os.path.exists(config_manta_fpath):
        manta_sv_fpath = search_sv_with_manta(main_ref_fpath, meta_ref_fpaths, output_dirpath, err_path)
        qutils.cat_files([manta_sv_fpath, trivial_deletions_fpath], bed_fpath)
    elif os.path.exists(trivial_deletions_fpath):
        shutil.copy(trivial_deletions_fpath, bed_fpath)

    if os.path.exists(bed_fpath):
        logger.main_info('  Structural variations saved to ' + bed_fpath)
        return bed_fpath, cov_fpath
    else:
        logger.main_info('  Failed searching structural variations.')
        return None, cov_fpath


def get_coverage(output_dirpath, ref_name, bam_fpath, err_path, cov_fpath):
    if not is_non_empty_file(cov_fpath):
        bamsorted_fpath = os.path.join(output_dirpath, ref_name + '.sorted')
        if not is_non_empty_file(bamsorted_fpath + '.bam'):
            qutils.call_subprocess([samtools_fpath('samtools'), 'sort',  '-@', str(qconfig.max_threads), bam_fpath,
                                    bamsorted_fpath], stdout=open(err_path, 'w'), stderr=open(err_path, 'a'))
        qutils.call_subprocess([samtools_fpath('samtools'), 'depth', bamsorted_fpath + '.bam'], stdout=open(cov_fpath, 'w'),
                               stderr=open(err_path, 'a'))
        qutils.assert_file_exists(cov_fpath, 'coverage file')
    return cov_fpath


def bin_fpath(fname):
    return os.path.join(bowtie_dirpath, fname)


def samtools_fpath(fname):
    return os.path.join(samtools_dirpath, fname)


def all_required_binaries_exist(bin_dirpath, binary):
    if not os.path.isfile(os.path.join(bin_dirpath, binary)):
        return False
    return True


def do(ref_fpath, contigs_fpaths, reads_fpaths, meta_ref_fpaths, output_dir, interleaved=False, external_logger=None, bed_fpath=None):
    if external_logger:
        global logger
        logger = external_logger
    logger.print_timestamp()
    logger.main_info('Running Structural Variants caller...')

    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    if not all_required_binaries_exist(bowtie_dirpath, 'bowtie2-align-l'):
        # making
        logger.main_info('Compiling Bowtie2 (details are in ' + os.path.join(bowtie_dirpath, 'make.log') + ' and make.err)')
        return_code = qutils.call_subprocess(
            ['make', '-C', bowtie_dirpath],
            stdout=open(os.path.join(bowtie_dirpath, 'make.log'), 'w'),
            stderr=open(os.path.join(bowtie_dirpath, 'make.err'), 'w'), logger=logger)

        if return_code != 0 or not all_required_binaries_exist(bowtie_dirpath, 'bowtie2-align-l'):
            logger.error('Failed to compile Bowtie2 (' + bowtie_dirpath + ')! '
                                                                   'Try to compile it manually. ' + (
                             'You can restart QUAST with the --debug flag '
                             'to see the command line.' if not qconfig.debug else ''))
            logger.main_info('Failed searching structural variations')
            return None, None

    if not all_required_binaries_exist(samtools_dirpath, 'samtools'):
        # making
        logger.main_info(
            'Compiling SAMtools (details are in ' + os.path.join(samtools_dirpath, 'make.log') + ' and make.err)')
        return_code = qutils.call_subprocess(
            ['make', '-C', samtools_dirpath],
            stdout=open(os.path.join(samtools_dirpath, 'make.log'), 'w'),
            stderr=open(os.path.join(samtools_dirpath, 'make.err'), 'w'), logger=logger)

        if return_code != 0 or not all_required_binaries_exist(samtools_dirpath, 'samtools'):
            logger.error('Failed to compile SAMtools (' + samtools_dirpath + ')! '
                                                                             'Try to compile it manually. ' + (
                             'You can restart QUAST with the --debug flag '
                             'to see the command line.' if not qconfig.debug else ''))
            logger.main_info('Failed searching structural variations')
            return None, None

    if not all_required_binaries_exist(manta_bin_dirpath, 'configManta.py'):
        # making
        if not os.path.exists(manta_build_dirpath):
            os.mkdir(manta_build_dirpath)
        if qconfig.platform_name == 'linux_64':
            logger.main_info('  Downloading binary distribution of Manta...')
            manta_downloaded_fpath = os.path.join(manta_build_dirpath, 'manta.tar.bz2')
            response = urllib2.urlopen(manta_download_path)
            content = response.read()
            if content:
                logger.main_info('  Manta successfully downloaded!')
                f = open(manta_downloaded_fpath + '.download', 'w' )
                f.write(content)
                f.close()
                if os.path.exists(manta_downloaded_fpath + '.download'):
                    logger.info('  Unpacking Manta...')
                    shutil.move(manta_downloaded_fpath + '.download', manta_downloaded_fpath)
                    import tarfile
                    tar = tarfile.open(manta_downloaded_fpath, "r:bz2")
                    tar.extractall(manta_build_dirpath)
                    tar.close()
                    manta_temp_dirpath = os.path.join(manta_build_dirpath, tar.members[0].name)
                    from distutils.dir_util import copy_tree
                    copy_tree(manta_temp_dirpath, manta_build_dirpath)
                    shutil.rmtree(manta_temp_dirpath)
                    os.remove(manta_downloaded_fpath)
                    logger.main_info('  Done')
            else:
                logger.main_info('  Failed downloading Manta from %s!' % manta_download_path)

        if not all_required_binaries_exist(manta_bin_dirpath, 'configManta.py'):
            logger.main_info('Compiling Manta (details are in ' + os.path.join(manta_dirpath, 'make.log') + ' and make.err)')
            prev_dir = os.getcwd()
            os.chdir(manta_build_dirpath)
            return_code = qutils.call_subprocess(
                [os.path.join(manta_dirpath, 'source', 'src', 'configure'), '--prefix=' + os.path.join(manta_dirpath, 'build'),
                 '--jobs=' + str(qconfig.max_threads)],
                stdout=open(os.path.join(manta_dirpath, 'make.log'), 'w'),
                stderr=open(os.path.join(manta_dirpath, 'make.err'), 'w'), logger=logger)
            if return_code == 0:
                return_code = qutils.call_subprocess(
                    ['make', '-j' + str(qconfig.max_threads)],
                    stdout=open(os.path.join(manta_dirpath, 'make.log'), 'a'),
                    stderr=open(os.path.join(manta_dirpath, 'make.err'), 'a'), logger=logger)
                if return_code == 0:
                    return_code = qutils.call_subprocess(
                    ['make', 'install'],
                    stdout=open(os.path.join(manta_dirpath, 'make.log'), 'a'),
                    stderr=open(os.path.join(manta_dirpath, 'make.err'), 'a'), logger=logger)
            os.chdir(prev_dir)
            if return_code != 0 or not all_required_binaries_exist(manta_bin_dirpath, 'configManta.py'):
                logger.warning('Failed to compile Manta (' + manta_dirpath + ')! '
                                                                       'Try to compile it manually ' + (
                                 'or download binary distribution from https://github.com/Illumina/manta/releases '
                                 'and unpack it into ' + os.path.join(manta_dirpath, 'build/') if qconfig.platform_name == 'linux_64' else '') + (
                                 '. You can restart QUAST with the --debug flag '
                                 'to see the command line.' if not qconfig.debug else '.'))
                logger.main_info('Failed searching structural variations. QUAST will search trivial deletions only.')

    temp_output_dir = os.path.join(output_dir, 'temp_output')

    if not os.path.isdir(temp_output_dir):
        os.mkdir(temp_output_dir)

    log_path = os.path.join(output_dir, 'sv_calling.log')
    err_path = os.path.join(output_dir, 'sv_calling.err')
    logger.info('  ' + 'Logging to files %s and %s...' % (log_path, err_path))
    try:
        bed_fpath, cov_fpath = run_processing_reads(ref_fpath, meta_ref_fpaths, contigs_analyzer.ref_labels_by_chromosomes,
                                                    reads_fpaths, temp_output_dir, output_dir, log_path, err_path, bed_fpath=bed_fpath)
    except:
        bed_fpath, cov_fpath = None, None
        logger.error('Failed searching structural variations! This function is experimental and may work improperly. Sorry for the inconvenience.')
    if not qconfig.debug:
        shutil.rmtree(temp_output_dir, ignore_errors=True)

    logger.info('Done.')
    return bed_fpath, cov_fpath
