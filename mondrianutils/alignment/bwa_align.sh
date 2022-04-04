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


bwa mem -C -M -t ${THREADS} \
-R "@RG\tID:${SAMPLE}_${LIBRARY}_${LANE}\tSM:${SAMPLE}\tLB:${LIBRARY}\tPL:ILLUMINA\tCN:${CENTRE}" \
${REFERENCE} ${FASTQ1} ${FASTQ2} | samtools sort -o ${OUTPUT} -
