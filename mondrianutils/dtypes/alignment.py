def dtypes(fastqscreen_genomes=['grch37', 'mm10', 'salmon']):
    metrics = {
        'cell_id': 'category',
        'total_mapped_reads': 'int',
        'library_id': 'category',
        'unpaired_mapped_reads': 'int',
        'paired_mapped_reads': 'int',
        'unpaired_duplicate_reads': 'int',
        'paired_duplicate_reads': 'int',
        'unmapped_reads': 'int',
        'percent_duplicate_reads': 'float',
        'estimated_library_size': 'int',
        'total_reads': 'int',
        'total_duplicate_reads': 'int',
        'total_properly_paired': 'int',
        'coverage_breadth': 'float',
        'coverage_depth': 'float',
        'median_insert_size': 'float',
        'mean_insert_size': 'float',
        'standard_deviation_insert_size': 'float',
        'cell_call': 'str',
        'column': 'int',
        'experimental_condition': 'str',
        'img_col': 'int',
        'index_i5': 'str',
        'index_i7': 'str',
        'primer_i5': 'str',
        'primer_i7': 'str',
        'row': 'int',
        'sample_type': 'str',
        'is_contaminated': 'bool',
        'trim': 'bool',
        'sample_id': 'category',
        'species': 'str',
        'condition': 'str',
        'index_sequence': 'str',
        'pick_met': 'str',
        'is_control': 'bool',
        'aligned': 'float',
        'expected': 'float',
        'overlap_with_all_filters': 'float',
        'overlap_with_all_filters_and_qual': 'float',
        'overlap_with_dups': 'float',
        'overlap_without_dups': 'float',
        'fastqscreen_nohit': 'int',
        'fastqscreen_total_reads': 'int',
        'fastqscreen_nohit_ratio': float,
        'tss_enrichment_score': float
    }

    for genome in fastqscreen_genomes:
        metrics['fastqscreen_{}'.format(genome)] = 'int'
        metrics['fastqscreen_{}_multihit'.format(genome)] = 'int'
        metrics['fastqscreen_{}_ratio'.format(genome)] = 'float'

    gc = {str(i): 'float' for i in range(0, 101)}
    gc['cell_id'] = 'category'

    fastqscreen_detailed = {
        'cell_id': 'category',
        'readend': 'str',
        'count': 'int'
    }
    for genome in fastqscreen_genomes:
        fastqscreen_detailed[genome] = 'int'


    dtypes = locals()

    return dtypes
