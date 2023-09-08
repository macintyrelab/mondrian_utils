import gzip
import json

import argparse
import math
import numpy as np
import pandas as pd
import pysam
import yaml
from mondrianutils import helpers

from . import consensus


def get_header(infile):
    with helpers.getFileHandle(infile, 'rt') as reader:
        header = []
        for line in reader:
            if line.startswith('#'):
                header.append(line)
            else:
                break
        return header


def merge_vcf_files(infiles, outfile):
    assert len(infiles) >= 1
    with helpers.getFileHandle(outfile, 'wt') as writer:
        header = get_header(infiles[0])
        for line in header:
            writer.write(line)

        for infile in infiles:
            with helpers.getFileHandle(infile, 'rt') as reader:
                for line in reader:
                    if line.startswith('#'):
                        continue
                    writer.write(line)


def generate_intervals(ref, chromosomes, size=1000000):

    if ref.endswith('.fai'):
        lengths = []
        names = []
        with open(ref, 'rt') as reader:
            for line in reader:
                line = line.strip().split()
                names.append(line[0])
                lengths.append(int(line[1]))
    else:
        fasta = pysam.FastaFile(ref)
        lengths = fasta.lengths
        names = fasta.references

    for name, length in zip(names, lengths):
        if name not in chromosomes:
            continue
        for i in range(int((length / size) + 1)):
            start = str(int(i * size) + 1)
            end = str(min(int((i + 1) * size), length))
            print(name + ":" + start + "-" + end)


def split_interval(interval, num_splits):
    chrom, coord = interval.split(':')
    start, end = coord.split('-')
    start = int(start)
    end = int(end)

    intervalsize = float(end - start) / num_splits
    intervalsize = int(math.ceil(intervalsize))

    intervals = []

    currpos = start
    for i in range(num_splits):
        interval_start = currpos
        interval_end = currpos + intervalsize
        currpos = interval_end + 1

        if interval_end >= end:
            intervals.append('{}:{}-{}'.format(chrom, interval_start, end))
            break
        else:
            intervals.append('{}:{}-{}'.format(chrom, interval_start, interval_end))

    for interval in intervals:
        print(interval)


def get_genome_size(ref, chromosomes):
    fasta = pysam.FastaFile(ref)
    lengths = fasta.lengths
    names = fasta.references

    genome_size = 0
    for name, length in zip(names, lengths):
        if name not in chromosomes:
            continue
        genome_size += length

    print(genome_size)


def merge_chromosome_depths_strelka(infiles, outfile):
    data = {}

    if isinstance(infiles, dict):
        infiles = infiles.values()

    for infile in infiles:
        with open(infile) as indata:
            depthdata = indata.readline()
            chrom, depth = depthdata.strip().split()
            if chrom not in data:
                data[chrom] = []
            data[chrom].append(float(depth))

    with open(outfile, 'w') as output:
        for chrom, depths in data.items():
            output.write('{}\t{}\n'.format(chrom, np.mean(depths)))


def get_sample_id_bam(bamfile):
    bam = pysam.AlignmentFile(bamfile)
    readgroups = bam.header['RG']

    samples = set()

    for readgroup in readgroups:
        samples.add(readgroup['SM'])

    assert len(samples) == 1

    print(list(samples)[0])


def _get_sample_id(bamfile):
    bam = pysam.AlignmentFile(bamfile)
    readgroups = bam.header['RG']

    samples = set()

    for readgroup in readgroups:
        samples.add(readgroup['SM'])

    assert len(samples) == 1

    return list(samples)[0]


def vcf_reheader_id(infile, outfile, tumour_bam, normal_bam, vcf_normal_id, vcf_tumour_id):
    tumour_id = _get_sample_id(tumour_bam)
    normal_id = _get_sample_id(normal_bam)

    in_opener = gzip.open if '.gz' in infile else open
    out_opener = gzip.open if '.gz' in outfile else open

    with in_opener(infile, 'rt') as indata:
        with out_opener(outfile, 'wt') as outdata:
            for line in indata:
                if line.startswith('#CHROM'):
                    outdata.write('##tumor_sample={}\n'.format(tumour_id))
                    outdata.write('##normal_sample={}\n'.format(normal_id))
                    line = line.replace(vcf_tumour_id, tumour_id).replace(vcf_normal_id, normal_id)
                    outdata.write(line)
                else:
                    outdata.write(line)


def update_maf_ids(infile, output, tumour_bam, normal_bam):
    normal_id = helpers.get_sample_from_bam(normal_bam)
    tumour_id = helpers.get_sample_from_bam(tumour_bam)

    with open(infile) as infile_read:
        maf_header = infile_read.readline()
    assert maf_header.startswith('#version 2.4')

    df = pd.read_csv(infile, skiprows=1, sep='\t')

    assert len(df['Tumor_Sample_Barcode'].unique()) == 1
    assert df['Tumor_Sample_Barcode'].unique()[0] == 'TUMOR'

    assert len(df['Matched_Norm_Sample_Barcode'].unique()) == 1
    assert df['Matched_Norm_Sample_Barcode'].unique()[0] == 'NORMAL'

    df['Matched_Norm_Sample_Barcode'] = normal_id

    # for germlines tumour will be none
    if tumour_id is None:
        tumour_id = 'NA'
    df['Tumor_Sample_Barcode'] = tumour_id

    with open(output, 'wt') as outfile:
        outfile.write(maf_header)

        df.to_csv(outfile, sep='\t', index=False)


def merge_mafs(infiles, output):
    assert len(infiles) > 0

    with helpers.getFileHandle(infiles[0], 'rt') as reader:
        header1 = reader.readline()
        header2 = reader.readline()
        assert header1.startswith('#version')
        assert header2.startswith('Hugo_Symbol')

    with helpers.getFileHandle(output, 'wt') as writer:
        writer.write(header1)
        writer.write(header2)

        for infile in infiles:
            with helpers.getFileHandle(infile, 'rt') as reader:
                for line in reader:
                    if line.startswith('#version') or line.startswith('Hugo_Symbol'):
                        continue
                    writer.write(line)


def update_maf_counts(input_maf, counts_file, output_maf):
    counts = {}
    with open(counts_file) as infile:
        for line in infile:
            line = line.strip().split()
            chrom, pos, id, ta, tr, td, na, nr, nd = line
            counts[(chrom, pos, id)] = (ta, tr, td, na, nr, nd)

    with open(input_maf) as infile, open(output_maf, 'wt') as outfile:
        header = infile.readline()
        assert header.startswith('#')
        outfile.write(header)

        header = infile.readline()
        outfile.write(header)

        header = {v: i for i, v in enumerate(header.strip().split('\t'))}
        t_dp = header['t_depth']
        t_ref = header['t_alt_count']
        t_alt = header['t_ref_count']
        n_dp = header['n_depth']
        n_ref = header['n_alt_count']
        n_alt = header['n_ref_count']

        chrom = header['Chromosome']
        pos = header['vcf_pos']
        vcfid = header['vcf_id']

        for line in infile:
            line_split = line.strip().split('\t')

            if (line_split[chrom], line_split[pos], line_split[vcfid]) in counts:
                ta, tr, td, na, nr, nd = counts[(line_split[chrom], line_split[pos], line_split[vcfid])]

                line_split[t_dp] = td
                line_split[t_ref] = tr
                line_split[t_alt] = ta

                line_split[n_dp] = nd
                line_split[n_ref] = nr
                line_split[n_alt] = na

                line = '\t'.join(line_split) + '\n'

            outfile.write(line)


def concatenate_csv(inputs, output):
    header = open(inputs[0]).readline()

    with open(output, 'wt') as outfile:
        outfile.write(header)
        for inputfile in inputs:
            with open(inputfile, 'rt') as infile:
                for line in infile:
                    if line.startswith('chrom'):
                        continue
                    outfile.write(line)


def fix_museq_vcf(infile, output):
    with open(infile, 'rt') as reader, open(output, 'wt') as writer:
        for line in reader:
            if line.startswith('#'):
                line = line.replace('##FORMAT=<ID=PL,Number=3', '##FORMAT=<ID=PL,Number=G')
            writer.write(line)


def infer_type(files):
    with open(files, 'rt') as reader:
        files = json.load(reader)

    filetypes = sorted(set([v['left'] for v in files]))

    # more than one wf
    if 'consensus_maf' in filetypes:
        return 'variant_calling'
    elif 'sample_consensus_vcf' in filetypes:
        return 'variant_consensus'
    elif 'mutect_vcf' in filetypes:
        return 'variant_mutect'
    elif 'museq_vcf' in filetypes:
        return 'variant_museq'
    elif 'strelka_indel' in filetypes:
        return 'variant_strelka'
    else:
        raise Exception()


def generate_metadata(
        files, metadata_yaml_files,
        samples, metadata_output
):
    wf_type = infer_type(files)
    data = helpers.metadata_helper(files, metadata_yaml_files, samples, wf_type)

    with open(metadata_output, 'wt') as writer:
        yaml.dump(data, writer, default_flow_style=False)


def parse_args():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    subparsers = parser.add_subparsers()

    genintervals = subparsers.add_parser("generate_intervals")
    genintervals.set_defaults(which='generate_intervals')
    genintervals.add_argument(
        "--reference",
        help='specify reference fasta'
    )
    genintervals.add_argument(
        "--chromosomes",
        nargs='*',
        default=list(map(str, range(1, 23))) + ['X', 'Y'],
        help='specify target chromosomes'
    )
    genintervals.add_argument(
        "--size",
        default=1000000,
        type=int,
        help='specify interval size'
    )

    split_interval = subparsers.add_parser("split_interval")
    split_interval.set_defaults(which='split_interval')
    split_interval.add_argument(
        "--interval",
        help='specify reference fasta'
    )
    split_interval.add_argument(
        "--num_splits",
        type=int,
        help='specify target chromosomes'
    )

    merge_vcf_files = subparsers.add_parser("merge_vcf_files")
    merge_vcf_files.set_defaults(which='merge_vcf_files')
    merge_vcf_files.add_argument(
        "--inputs", nargs='*',
        help='vcf files to merge'
    )
    merge_vcf_files.add_argument(
        "--output",
        help='merged output vcf'
    )

    genome_size = subparsers.add_parser("genome_size")
    genome_size.set_defaults(which='genome_size')
    genome_size.add_argument(
        "--reference",
        help='specify reference fasta'
    )
    genome_size.add_argument(
        "--chromosomes",
        nargs='*',
        default=list(map(str, range(1, 23))) + ['X', 'Y'],
        help='specify target chromosomes'
    )

    merge_chromosome_depths_strelka = subparsers.add_parser("merge_chromosome_depths_strelka")
    merge_chromosome_depths_strelka.set_defaults(which='merge_chromosome_depths_strelka')
    merge_chromosome_depths_strelka.add_argument(
        "--inputs",
        nargs='*',
        help='specify input'
    )
    merge_chromosome_depths_strelka.add_argument(
        "--output",
        help='specify output'
    )

    get_sid_bam = subparsers.add_parser("get_sample_id_bam")
    get_sid_bam.set_defaults(which='get_sample_id_bam')
    get_sid_bam.add_argument(
        "--input",
        help='specify input bam'
    )

    consensus = subparsers.add_parser('consensus')
    consensus.set_defaults(which='consensus')
    consensus.add_argument('--museq_vcf', required=True)
    consensus.add_argument('--mutect_vcf', required=True)
    consensus.add_argument('--strelka_indel', required=True)
    consensus.add_argument('--strelka_snv', required=True)
    consensus.add_argument('--consensus_output', required=True)
    consensus.add_argument('--counts_output', required=True)
    consensus.add_argument('--chromosomes', default=[str(v) for v in range(1, 23)] + ['X', 'Y'], nargs='*')

    vcf_reheader_id = subparsers.add_parser('vcf_reheader_id')
    vcf_reheader_id.set_defaults(which='vcf_reheader_id')
    vcf_reheader_id.add_argument('--input', required=True)
    vcf_reheader_id.add_argument('--output', required=True)
    vcf_reheader_id.add_argument('--tumour', required=True)
    vcf_reheader_id.add_argument('--normal', required=True)
    vcf_reheader_id.add_argument('--vcf_normal_id', required=True)
    vcf_reheader_id.add_argument('--vcf_tumour_id', required=True)

    update_maf_id = subparsers.add_parser('update_maf_ids')
    update_maf_id.set_defaults(which='update_maf_ids')
    update_maf_id.add_argument('--input', required=True)
    update_maf_id.add_argument('--output', required=True)
    update_maf_id.add_argument('--tumour_bam')
    update_maf_id.add_argument('--normal_bam')

    update_maf_id = subparsers.add_parser('update_maf_counts')
    update_maf_id.set_defaults(which='update_maf_counts')
    update_maf_id.add_argument('--input', required=True)
    update_maf_id.add_argument('--counts', required=True)
    update_maf_id.add_argument('--output', required=True)

    merge_mafs = subparsers.add_parser('merge_mafs')
    merge_mafs.set_defaults(which='merge_mafs')
    merge_mafs.add_argument('--infiles', nargs='*', required=True)
    merge_mafs.add_argument('--output', required=True)

    concat_csv_parser = subparsers.add_parser('concat_csv')
    concat_csv_parser.set_defaults(which='concat_csv')
    concat_csv_parser.add_argument('--inputs', nargs='*', required=True)
    concat_csv_parser.add_argument('--output', required=True)

    fix_museq_vcf = subparsers.add_parser('fix_museq_vcf')
    fix_museq_vcf.set_defaults(which='fix_museq_vcf')
    fix_museq_vcf.add_argument('--input', required=True)
    fix_museq_vcf.add_argument('--output', required=True)

    merge_bams = subparsers.add_parser('merge_bams')
    merge_bams.set_defaults(which='merge_bams')
    merge_bams.add_argument('--inputs', nargs="*", required=True)
    merge_bams.add_argument('--output', required=True)
    merge_bams.add_argument('--threads', type=int, required=True)
    merge_bams.add_argument('--tempdir', required=True)

    generate_metadata = subparsers.add_parser('generate_metadata')
    generate_metadata.set_defaults(which='generate_metadata')
    generate_metadata.add_argument(
        '--files'
    )
    generate_metadata.add_argument(
        '--metadata_yaml_files', nargs='*'
    )
    generate_metadata.add_argument(
        '--samples', nargs='*'
    )
    generate_metadata.add_argument(
        '--metadata_output'
    )

    args = vars(parser.parse_args())

    return args


def utils():
    args = parse_args()

    if args['which'] == 'generate_intervals':
        generate_intervals(args['reference'], args['chromosomes'], args['size'])
    elif args['which'] == 'split_interval':
        split_interval(args['interval'], args['num_splits'])
    elif args['which'] == 'genome_size':
        get_genome_size(args['reference'], args['chromosomes'])
    elif args['which'] == 'merge_chromosome_depths_strelka':
        merge_chromosome_depths_strelka(args['inputs'], args['output'])
    elif args['which'] == 'get_sample_id_bam':
        get_sample_id_bam(args['input'])
    elif args['which'] == 'vcf_reheader_id':
        vcf_reheader_id(args['input'], args['output'], args['tumour'], args['normal'], args['vcf_normal_id'],
                        args['vcf_tumour_id'])
    elif args['which'] == 'update_maf_ids':
        update_maf_ids(args['input'], args['output'], args['tumour_bam'], args['normal_bam'])
    elif args['which'] == 'merge_mafs':
        merge_mafs(args['infiles'], args['output'])
    elif args['which'] == 'update_maf_counts':
        update_maf_counts(args['input'], args['counts'], args['output'])
    elif args['which'] == 'concat_csv':
        concatenate_csv(args['inputs'], args['output'])
    elif args['which'] == 'fix_museq_vcf':
        fix_museq_vcf(args['input'], args['output'])
    elif args['which'] == 'consensus':
        consensus.main(
            args['museq_vcf'], args['strelka_snv'], args['mutect_vcf'], args['strelka_indel'],
            args['consensus_output'], args['counts_output'],
            args['chromosomes']
        )
    elif args['which'] == 'generate_metadata':
        generate_metadata(
            args['files'], args['metadata_yaml_files'],
            args['samples'], args['metadata_output']
        )
    elif args['which'] == 'merge_vcf_files':
        merge_vcf_files(args['inputs'], args['output'])
    elif args['which'] == 'merge_bams':
        helpers.merge_bams(args['inputs'], args['output'], args['tempdir'], args['threads'])
    else:
        raise Exception()
