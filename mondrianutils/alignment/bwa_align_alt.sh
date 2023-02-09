#!/bin/bash

FASTQ1=$1
FASTQ2=$2
REFERENCE=$3
OUTPUT=$4
SAMPLE=$5
LIBRARY=$6
CELL=$7
LANE=$8
FLOWCELL=$9
CENTRE=${10}
THREADS=${11}
TEMPDIR=${12}


bwa mem -C -M -t ${THREADS} \
-R "@RG\tID:${SAMPLE}_${LIBRARY}_${FLOWCELL}_${LANE}\tSM:${SAMPLE}\tLB:${LIBRARY}\tPU:${LANE}_${FLOWCELL}\tPL:ILLUMINA\tCN:${CENTRE}" \
${REFERENCE} ${FASTQ1} ${FASTQ2} > ${TEMPDIR}/aligned.sam

bwa-postalt.js ${REFERENCE} ${TEMPDIR}/aligned.sam > ${TEMPDIR}/aligned_postalt.sam

samtools sort -o ${OUTPUT} ${TEMPDIR}/aligned_postalt.sam
