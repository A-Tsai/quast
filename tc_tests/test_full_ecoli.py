#!/usr/bin/python

import os
from common import *

name = os.path.basename(__file__)[5:-3]
contigs = ['ecoli/%s.fasta.gz' % i for i in ['ABySS', 'EULER-SR', 'SPAdes', 'Velvet']]


run_quast(name, contigs=contigs, params='-R reference.fa -G genes.txt -t 2')
check_report_files(name)
assert_report_header(name, contigs=contigs)
assert_metric(name, 'N50', ['76520', '29342', '109140', '32469'])
assert_metric(name, '# misassemblies', ['7', '14', '1', '2'])
assert_metric(name, '# local misassemblies', ['1', '142', '9', '1'])
assert_metric(name, 'Genome fraction (%)', ['88.274', '85.357', '94.888', '73.764'])