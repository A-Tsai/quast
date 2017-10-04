./pbsim --prefix pb-1 --depth 0.1 --sample-fastq m131017_060208_42213_c100579642550000001823095604021496_s1_p0.1.subreads.fastq --length-min 1000 --length-max 30000 --seed 11 hs38.fa

bin/mason_variator -ir hs38.fa -s 1 -ov hs38-s1.vcf --snp-rate 1e-3 --small-indel-rate 2e-4 --sv-indel-rate 0 --sv-inversion-rate 0 --sv-translocation-rate 0 --sv-duplication-rate 0 --max-small-indel-size 10
bin/mason_simulator -ir hs38.fa -iv hs38-s1.vcf -n 1000000 --seed 1 -o s1_1.fq -or s1_2.fq -oa s1.sam --illumina-prob-mismatch-scale 2.5
