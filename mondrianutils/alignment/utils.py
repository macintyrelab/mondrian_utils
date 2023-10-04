import json
import os
import subprocess
from collections import defaultdict

import argparse
import csverve.api as csverve
import mondrianutils.helpers as helpers
import pysam
import yaml
from mondrianutils import __version__
from mondrianutils.alignment.collect_gc_metrics import collect_gc_metrics
from mondrianutils.alignment.collect_metrics import collect_metrics
from mondrianutils.alignment.complete_alignment import alignment
from mondrianutils.alignment.coverage_metrics import get_coverage_metrics
from mondrianutils.dtypes.alignment import dtypes
from mondrianutils.alignment.fastqscreen import merge_fastq_screen_counts
from mondrianutils.alignment.fastqscreen import organism_filter
from mondrianutils.alignment.trim_galore import trim_galore


class MultipleSamplesPerRun(Exception):
    pass


class MissingField(Exception):
    pass


def _check_sample_id_uniqueness(meta_data):
    non_control_samples = [v['sample_id'] for k, v in meta_data['cells'].items() if v['is_control'] == False]
    non_control_samples = sorted(set(non_control_samples))

    # allow 0 sample ids in case all cells are control
    if not len(non_control_samples) <= 1:
        raise MultipleSamplesPerRun(
            f'only one sample id expected in non control cells, found {non_control_samples}'
        )


def _check_metadata_required_field(meta_data, field_name):
    try:
        [v[field_name] for k, v in meta_data['cells'].items()]
    except KeyError:
        raise MissingField(f'{field_name} is required for each cell in meta section of metadata input yaml')


def _check_lanes_and_flowcells(meta_data, input_data):
    lane_data = defaultdict(set)

    for val in input_data:
        for lane in val['lanes']:
            lane_data[lane['flowcell_id']].add(lane['lane_id'])

    for flowcell, lanes in lane_data.items():
        if flowcell not in meta_data['lanes']:
            raise MissingField(
                f'missing flowcell {flowcell} in metadata yaml'
            )
        for lane_id in lanes:
            if lane_id not in meta_data['lanes'][flowcell]:
                raise MissingField(
                    f'missing lane {lane_id} for flowcell {flowcell} in metadata yaml'
                )


def input_validation(meta_yaml, input_json):
    with open(meta_yaml, 'rt') as reader:
        meta_data = yaml.safe_load(reader)
        meta_data = meta_data['meta']

    with open(input_json, 'rt') as reader:
        input_data = json.load(reader)

    _check_metadata_required_field(meta_data, 'is_control')
    _check_metadata_required_field(meta_data, 'library_id')
    _check_metadata_required_field(meta_data, 'sample_id')
    # these are required in hmmcopy
    _check_metadata_required_field(meta_data, 'pick_met')
    _check_metadata_required_field(meta_data, 'condition')

    _check_sample_id_uniqueness(meta_data)

    _check_lanes_and_flowcells(meta_data, input_data)

    for flowcell_id, all_lane_data in meta_data['lanes'].items():
        for lane_id, lane_data in all_lane_data.items():
            if 'sequencing_centre' not in lane_data:
                raise MissingField(
                    f'sequencing centre missing for flowcell {flowcell_id} lane {lane_id}'
                )


def get_cell_id_from_bam(infile):
    infile = pysam.AlignmentFile(infile, "rb")

    iter = infile.fetch(until_eof=True)
    for read in iter:
        return read.get_tag('CB')


def get_new_header(cells, bamfile, new_header):
    subprocess.run(['samtools', 'view', '-H', bamfile, '-o', new_header])
    with open(new_header, 'at') as header:
        for cell in cells:
            header.write('@CO\tCB:{}\n'.format(cell))


def reheader(infile, new_header, outfile):
    subprocess.run(
        ['picard', 'ReplaceSamHeader', 'I={}'.format(infile),
         'HEADER={}'.format(new_header), 'O={}'.format(outfile)
         ]
    )


def get_pass_files(infiles, cell_ids, metrics):
    metrics = csverve.read_csv(metrics)
    assert set(cell_ids) == set(list(metrics['cell_id']))

    cells_to_skip = set(list(metrics[metrics['is_contaminated']]['cell_id']))
    infiles = {cell: infile for cell, infile in zip(cell_ids, infiles) if cell not in cells_to_skip}

    cells_to_skip = set(list(metrics[metrics['is_control']]['cell_id']))
    infiles = {cell: infile for cell, infile in infiles.items() if cell not in cells_to_skip}

    return infiles


def get_control_files(infiles, cell_ids, metrics):
    metrics = csverve.read_csv(metrics)
    assert set(cell_ids) == set(list(metrics['cell_id']))
    control_cells = set(list(metrics[metrics['is_control'] == True]['cell_id']))
    infiles = {cell: infile for cell, infile in zip(cell_ids, infiles) if cell in control_cells}
    return infiles


def get_contaminated_files(infiles, cell_ids, metrics):
    metrics = csverve.read_csv(metrics)
    assert set(cell_ids) == set(list(metrics['cell_id']))

    cells_to_skip = set(list(metrics[metrics['is_control']]['cell_id']))
    infiles = {cell: infile for cell, infile in zip(cell_ids, infiles) if cell not in cells_to_skip}

    contaminated_cells = set(list(metrics[metrics['is_contaminated']]['cell_id']))
    infiles = {cell: infile for cell, infile in infiles.items() if cell in contaminated_cells}

    return infiles


def samtools_index(infile):
    cmd = ['samtools', 'index', infile]
    helpers.run_cmd(cmd)


def igvtools_count(infile, reference):
    cmd = ['igvtools', 'count', infile, infile+'.tdf', reference]
    helpers.run_cmd(cmd)


def merge_cells(infiles, tempdir, ncores, outfile, reference, empty_bam_content):
    if len(infiles.values()) == 0:
        pysam.AlignmentFile(outfile, "wb", header=empty_bam_content).close()
    else:
        final_merge_output = os.path.join(tempdir, 'merged_all.bam')
        helpers.merge_bams(list(infiles.values()), final_merge_output, tempdir, ncores)

        new_header = os.path.join(tempdir, 'header.sam')
        get_new_header(infiles.keys(), final_merge_output, new_header)

        reheader(final_merge_output, new_header, outfile)

    samtools_index(outfile)
    igvtools_count(outfile, reference)


def get_bam_header(bam):

    infile = pysam.AlignmentFile(bam, "rb")

    header = infile.header

    if 'CO' in header:
        del header['CO']

    return header


def generate_bams(
        infiles, reference, cell_ids, metrics,
        control_outfile, contaminated_outfile, pass_outfile,
        tempdir, ncores
):
    header = get_bam_header(infiles[0])
    # controls
    control_bams = get_control_files(infiles, cell_ids, metrics)
    control_tempdir = os.path.join(tempdir, 'control')
    helpers.makedirs(control_tempdir)
    merge_cells(control_bams, control_tempdir, ncores, control_outfile, reference, header)

    # contaminated
    contaminated_bams = get_contaminated_files(infiles, cell_ids, metrics)
    contaminated_tempdir = os.path.join(tempdir, 'contaminated')
    helpers.makedirs(contaminated_tempdir)
    merge_cells(contaminated_bams, contaminated_tempdir, ncores, contaminated_outfile, reference, header)

    # pass
    pass_bams = get_pass_files(infiles, cell_ids, metrics)
    pass_tempdir = os.path.join(tempdir, 'pass')
    helpers.makedirs(pass_tempdir)
    merge_cells(pass_bams, pass_tempdir, ncores, pass_outfile, reference, header)


def tag_bam_with_cellid(infile, outfile, cell_id):
    infile = pysam.AlignmentFile(infile, "rb")
    outfile = pysam.AlignmentFile(outfile, "wb", template=infile)

    iter = infile.fetch(until_eof=True)
    for read in iter:
        read.set_tag('CB', cell_id, replace=False)
        outfile.write(read)
    infile.close()
    outfile.close()


def _get_col_data(df, organism):
    return df['fastqscreen_{}'.format(organism)] - df['fastqscreen_{}_multihit'.format(organism)]


def add_contamination_status(
        infile, outfile,
        reference, threshold=0.05
):
    data = csverve.read_csv(infile)

    data = data.set_index('cell_id', drop=False)

    organisms = [v for v in data.columns.values if v.startswith('fastqscreen_')]
    organisms = sorted(set([v.split('_')[1] for v in organisms]))
    organisms = [v for v in organisms if v not in ['nohit', 'total']]

    if reference not in organisms:
        raise Exception("Could not find the fastq screen counts")

    alts = [col for col in organisms if not col == reference]

    data['is_contaminated'] = False

    for altcol in alts:
        perc_alt = _get_col_data(data, altcol) / data['fastqscreen_total_reads']
        data.loc[perc_alt > threshold, 'is_contaminated'] = True

    col_type = dtypes()['metrics']['is_contaminated']

    data['is_contaminated'] = data['is_contaminated'].astype(col_type)
    csverve.write_dataframe_to_csv_and_yaml(
        data, outfile, dtypes(fastqscreen_genomes=organisms)['metrics']
    )


def add_metadata(metrics, metadata_yaml, output):
    df = csverve.read_csv(metrics)

    metadata = yaml.safe_load(open(metadata_yaml, 'rt'))

    cells = metadata['meta']['cells'].keys()

    assert set(cells) == set(df['cell_id'])

    for cellid, cell_info in metadata['meta']['cells'].items():
        for colname, val in cell_info.items():
            df.loc[df['cell_id'] == cellid, colname] = val

    organisms = [v for v in df.columns.values if v.startswith('fastqscreen_')]
    organisms = sorted(set([v.split('_')[1] for v in organisms]))
    organisms = [v for v in organisms if v not in ['nohit', 'total']]

    csverve.write_dataframe_to_csv_and_yaml(
        df, output,
        dtypes=dtypes(fastqscreen_genomes=organisms)['metrics']
    )


def generate_metadata(
        bam, control, contaminated, metrics, gc_metrics,
        fastqscreen, tarfile, metadata_input, metadata_output
):
    with open(metadata_input, 'rt') as reader:
        data = yaml.safe_load(reader)

    lane_data = data['meta']['lanes']

    samples = set()
    libraries = set()
    cells = []
    for cell in data['meta']['cells']:
        cells.append(cell)
        samples.add(data['meta']['cells'][cell]['sample_id'])
        libraries.add(data['meta']['cells'][cell]['library_id'])

    data = dict()
    data['files'] = {
        os.path.basename(metrics[0]): {
            'result_type': 'alignment_metrics',
            'auxiliary': helpers.get_auxiliary_files(metrics[0])
        },
        os.path.basename(metrics[1]): {
            'result_type': 'alignment_metrics',
            'auxiliary': helpers.get_auxiliary_files(metrics[1])
        },
        os.path.basename(gc_metrics[0]): {
            'result_type': 'alignment_gc_metrics',
            'auxiliary': helpers.get_auxiliary_files(gc_metrics[0])
        },
        os.path.basename(gc_metrics[1]): {
            'result_type': 'alignment_gc_metrics',
            'auxiliary': helpers.get_auxiliary_files(gc_metrics[1])
        },
        os.path.basename(bam[0]): {
            'result_type': 'merged_cells_bam', 'filtering': 'passed',
            'auxiliary': helpers.get_auxiliary_files(bam[0])
        },
        os.path.basename(bam[1]): {
            'result_type': 'merged_cells_bam', 'filtering': 'passed',
            'auxiliary': helpers.get_auxiliary_files(bam[1])
        },
        os.path.basename(control[0]): {
            'result_type': 'merged_cells_bam', 'filtering': 'control',
            'auxiliary': helpers.get_auxiliary_files(control[0])
        },
        os.path.basename(control[1]): {
            'result_type': 'merged_cells_bam', 'filtering': 'control',
            'auxiliary': helpers.get_auxiliary_files(control[1])
        },
        os.path.basename(contaminated[0]): {
            'result_type': 'merged_cells_bam', 'filtering': 'contaminated',
            'auxiliary': helpers.get_auxiliary_files(contaminated[0])
        },
        os.path.basename(contaminated[1]): {
            'result_type': 'merged_cells_bam', 'filtering': 'contaminated',
            'auxiliary': helpers.get_auxiliary_files(contaminated[1])
        },
        os.path.basename(fastqscreen[0]): {
            'result_type': 'detailed_fastqscreen_breakdown',
            'auxiliary': helpers.get_auxiliary_files(fastqscreen[0])
        },
        os.path.basename(fastqscreen[1]): {
            'result_type': 'detailed_fastqscreen_breakdown',
            'auxiliary': helpers.get_auxiliary_files(fastqscreen[1])
        },
        os.path.basename(tarfile): {
            'result_type': 'alignment_metrics_plots',
            'auxiliary': helpers.get_auxiliary_files(tarfile)
        }
    }

    data['meta'] = {
        'type': 'alignment',
        'version': __version__,
        'sample_ids': sorted(samples),
        'library_ids': sorted(libraries),
        'cell_ids': sorted(cells),
        'lane_ids': lane_data
    }

    with open(metadata_output, 'wt') as writer:
        yaml.dump(data, writer, default_flow_style=False)


def _json_file_parser(filepath):
    return json.load(open(filepath, 'rt'))


def parse_args():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    subparsers = parser.add_subparsers()

    fastqscreen = subparsers.add_parser('fastqscreen')
    fastqscreen.set_defaults(which='fastqscreen')
    fastqscreen.add_argument(
        "--r1",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--r2",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--output_r1",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--output_r2",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--detailed_metrics",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--summary_metrics",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--tempdir",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--cell_id",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--human_reference",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--mouse_reference",
        help='specify reference fasta'
    )
    fastqscreen.add_argument(
        "--salmon_reference",
        help='specify reference fasta'
    )

    merge_fastqscreen_counts = subparsers.add_parser('merge_fastqscreen_counts')
    merge_fastqscreen_counts.set_defaults(which='merge_fastqscreen_counts')
    merge_fastqscreen_counts.add_argument(
        '--detailed_counts',
        nargs='*'
    )
    merge_fastqscreen_counts.add_argument(
        '--summary_counts',
        nargs='*'
    )
    merge_fastqscreen_counts.add_argument(
        '--merged_detailed'
    )
    merge_fastqscreen_counts.add_argument(
        '--merged_summary',
    )

    collect_metrics = subparsers.add_parser('collect_metrics')
    collect_metrics.set_defaults(which='collect_metrics')
    collect_metrics.add_argument(
        '--wgs_metrics',
    )
    collect_metrics.add_argument(
        '--insert_metrics',
    )
    collect_metrics.add_argument(
        '--flagstat',
    )
    collect_metrics.add_argument(
        '--markdups_metrics',
    )
    collect_metrics.add_argument(
        '--coverage_metrics',
    )
    collect_metrics.add_argument(
        '--output',
    )
    collect_metrics.add_argument(
        '--cell_id',
    )

    collect_gc_metrics = subparsers.add_parser('collect_gc_metrics')
    collect_gc_metrics.set_defaults(which='collect_gc_metrics')
    collect_gc_metrics.add_argument(
        '--infile',
    )
    collect_gc_metrics.add_argument(
        '--outfile',
    )
    collect_gc_metrics.add_argument(
        '--cell_id',
    )

    tag_bam = subparsers.add_parser('tag_bam_with_cellid')
    tag_bam.set_defaults(which='tag_bam_with_cellid')
    tag_bam.add_argument(
        '--infile',
    )
    tag_bam.add_argument(
        '--outfile',
    )
    tag_bam.add_argument(
        '--cell_id',
    )

    contamination_status = subparsers.add_parser('add_contamination_status')
    contamination_status.set_defaults(which='add_contamination_status')
    contamination_status.add_argument(
        '--infile',
    )
    contamination_status.add_argument(
        '--outfile',
    )
    contamination_status.add_argument(
        '--reference'
    )

    merge_cells = subparsers.add_parser('merge_cells')
    merge_cells.set_defaults(which='merge_cells')
    merge_cells.add_argument(
        '--infiles', nargs='*'
    )
    merge_cells.add_argument(
        '--cell_ids', nargs='*'
    )
    merge_cells.add_argument(
        '--reference',
    )
    merge_cells.add_argument(
        '--control_outfile',
    )
    merge_cells.add_argument(
        '--contaminated_outfile',
    )
    merge_cells.add_argument(
        '--pass_outfile',
    )
    merge_cells.add_argument(
        '--metrics',
    )
    merge_cells.add_argument(
        '--tempdir',
    )
    merge_cells.add_argument(
        '--ncores',
        type=int
    )

    classifier = subparsers.add_parser('classify_fastqscreen')
    classifier.set_defaults(which='classify_fastqscreen')
    classifier.add_argument(
        '--training_data'
    )
    classifier.add_argument(
        '--metrics'
    )
    classifier.add_argument(
        '--output',
    )

    coverage_metrics = subparsers.add_parser('coverage_metrics')
    coverage_metrics.set_defaults(which='coverage_metrics')
    coverage_metrics.add_argument(
        '--metrics'
    )
    coverage_metrics.add_argument(
        '--bamfile'
    )
    coverage_metrics.add_argument(
        '--output',
    )

    get_sample_id = subparsers.add_parser('get_sample_id')
    get_sample_id.set_defaults(which='get_sample_id')
    get_sample_id.add_argument(
        '--metadata_yaml'
    )
    get_sample_id.add_argument(
        '--cell_id'
    )

    get_library_id = subparsers.add_parser('get_library_id')
    get_library_id.set_defaults(which='get_library_id')
    get_library_id.add_argument(
        '--metadata_yaml'
    )
    get_library_id.add_argument(
        '--cell_id'
    )

    generate_metadata = subparsers.add_parser('generate_metadata')
    generate_metadata.set_defaults(which='generate_metadata')
    generate_metadata.add_argument(
        '--metrics', nargs=2
    )
    generate_metadata.add_argument(
        '--gc_metrics', nargs=2
    )
    generate_metadata.add_argument(
        '--bam', nargs=2
    )
    generate_metadata.add_argument(
        '--control', nargs=2
    )
    generate_metadata.add_argument(
        '--contaminated', nargs=2
    )
    generate_metadata.add_argument(
        '--fastqscreen_detailed', nargs=2
    )
    generate_metadata.add_argument(
        '--tarfile'
    )
    generate_metadata.add_argument(
        '--metadata_input'
    )
    generate_metadata.add_argument(
        '--metadata_output'
    )

    add_metadata = subparsers.add_parser('add_metadata')
    add_metadata.set_defaults(which='add_metadata')
    add_metadata.add_argument(
        '--metrics'
    )
    add_metadata.add_argument(
        '--metadata_yaml'
    )
    add_metadata.add_argument(
        '--output',
    )

    trim_galore = subparsers.add_parser('trim_galore')
    trim_galore.set_defaults(which='trim_galore')
    trim_galore.add_argument(
        '--r1'
    )
    trim_galore.add_argument(
        '--r2'
    )
    trim_galore.add_argument(
        '--output_r1',
    )
    trim_galore.add_argument(
        '--output_r2',
    )
    trim_galore.add_argument(
        '--adapter1',
    )
    trim_galore.add_argument(
        '--adapter2',
    )
    trim_galore.add_argument(
        '--tempdir',
    )

    bwa_align = subparsers.add_parser('bwa_align')
    bwa_align.set_defaults(which='bwa_align')
    bwa_align.add_argument(
        '--metadata_yaml'
    )
    bwa_align.add_argument(
        '--reference'
    )
    bwa_align.add_argument(
        '--output',
    )
    bwa_align.add_argument(
        '--fastq1',
    )
    bwa_align.add_argument(
        '--fastq2',
    )
    bwa_align.add_argument(
        '--lane_id',
    )
    bwa_align.add_argument(
        '--flowcell_id',
    )
    bwa_align.add_argument(
        '--cell_id',
    )

    alignment = subparsers.add_parser('alignment')
    alignment.set_defaults(which='alignment')
    alignment.add_argument(
        '--fastq_files'
    )
    alignment.add_argument(
        '--num_threads',
        default=1
    )
    alignment.add_argument(
        '--metadata_yaml'
    )
    alignment.add_argument(
        '--reference'
    )
    alignment.add_argument(
        '--reference_name'
    )
    alignment.add_argument(
        '--supplementary_references_json'
    )
    alignment.add_argument(
        '--tempdir',
    )
    alignment.add_argument(
        '--adapter1',
    )
    alignment.add_argument(
        '--adapter2',
    )
    alignment.add_argument(
        '--cell_id',
    )
    alignment.add_argument(
        '--wgs_metrics_mqual',
    )
    alignment.add_argument(
        '--wgs_metrics_bqual',
    )
    alignment.add_argument(
        '--wgs_metrics_count_unpaired',
    )
    alignment.add_argument(
        '--bam_output',
    )
    alignment.add_argument(
        '--metrics_output',
    )
    alignment.add_argument(
        '--metrics_gc_output',
    )
    alignment.add_argument(
        '--fastqscreen_detailed_output',
    )
    alignment.add_argument(
        '--fastqscreen_summary_output',
    )
    alignment.add_argument(
        '--tar_output',
    )
    alignment.add_argument(
        '--run_fastqc',
        default=False,
        action='store_true'
    )

    input_validation = subparsers.add_parser('input_validation')
    input_validation.set_defaults(which='input_validation')
    input_validation.add_argument(
        '--meta_yaml', required=True
    )
    input_validation.add_argument(
        '--input_data_json', required=True
    )
    args = vars(parser.parse_args())

    return args


def utils():
    args = parse_args()

    if args['which'] == 'fastqscreen':
        organism_filter(
            args['r1'], args['r2'], args['output_r1'], args['output_r2'],
            args['detailed_metrics'], args['summary_metrics'], args['tempdir'],
            args['cell_id'], args['human_reference'],
            args['mouse_reference'], args['salmon_reference'])
    elif args['which'] == 'merge_fastqscreen_counts':
        merge_fastq_screen_counts(
            args['detailed_counts'], args['summary_counts'],
            args['merged_detailed'], args['merged_summary']
        )
    elif args['which'] == 'collect_metrics':
        collect_metrics(
            args['wgs_metrics'], args['insert_metrics'],
            args['flagstat'], args['markdups_metrics'],
            args['coverage_metrics'], args['output'],
            args['cell_id']
        )
    elif args['which'] == 'collect_gc_metrics':
        collect_gc_metrics(
            args['infile'], args['outfile'], args['cell_id']
        )

    elif args['which'] == 'tag_bam_with_cellid':
        tag_bam_with_cellid(
            args['infile'], args['outfile'],
            args['cell_id']
        )
    elif args['which'] == 'add_contamination_status':
        add_contamination_status(
            args['infile'], args['outfile'],
            args['reference']
        )
    elif args['which'] == 'merge_cells':
        generate_bams(
            args['infiles'], args['reference'], args['cell_ids'], args['metrics'],
            args['control_outfile'], args['contaminated_outfile'],
            args['pass_outfile'], args['tempdir'], args['ncores']
        )
    # elif args['which'] == 'classify_fastqscreen':
    #     classify_fastqscreen(
    #         args['training_data'], args['metrics'], args['output']
    #     )
    elif args['which'] == 'coverage_metrics':
        get_coverage_metrics(args['bamfile'], args['output'])
    elif args['which'] == 'add_metadata':
        add_metadata(
            args['metrics'], args['metadata_yaml'], args['output']
        )
    elif args['which'] == 'generate_metadata':
        generate_metadata(
            args['bam'], args['control'], args['contaminated'], args['metrics'], args['gc_metrics'],
            args['fastqscreen_detailed'], args['tarfile'], args['metadata_input'], args['metadata_output']
        )
    elif args['which'] == 'trim_galore':
        trim_galore(
            args['r1'], args['r2'], args['output_r1'], args['output_r2'],
            args['adapter1'], args['adapter2'], args['tempdir']
        )
    elif args['which'] == 'alignment':
        alignment(
            args['fastq_files'], args['metadata_yaml'], args['reference'],
            args['reference_name'], args['supplementary_references_json'], args['tempdir'],
            args['adapter1'], args['adapter2'], args['cell_id'], args['wgs_metrics_mqual'],
            args['wgs_metrics_bqual'], args['wgs_metrics_count_unpaired'],
            args['bam_output'], args['metrics_output'], args['metrics_gc_output'],
            args['fastqscreen_detailed_output'], args['fastqscreen_summary_output'],
            args['tar_output'], args['num_threads'], run_fastqc=args['run_fastqc']
        )
    elif args['which'] == 'input_validation':
        input_validation(args['meta_yaml'], args['input_data_json'])
    else:
        raise Exception()
