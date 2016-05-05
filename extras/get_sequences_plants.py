from bioservices import biomart
import pandas as pd
import io
import requests
import sys
from Bio import SeqIO


# Load annotation
gene_table_file = sys.argv[1]
gene_table = pd.read_csv(gene_table_file)
gene_table.set_index('acc',inplace=True)
# Define column data types
gene_table['chromosome'] = gene_table['chromosome'].astype(str)
for col in ['start','end','length','strand']:
    gene_table[col] = gene_table[col].astype(int)

# Number of genes to be analyzed at a time
chunk_size = int(sys.argv[2])
# Setup server
b = biomart.BioMart()
b.host = 'plants.ensembl.org'

datasets = b.datasets("plants_mart")

# Set sequence file name
seq_file = 'all_seqs.fa'
fileO = open(seq_file,'a')
# Record downloaded sequences
downloaded = [gene.name.split("|")[2] for gene in SeqIO.parse(seq_file,'fasta')]

# Filter table so that only non downloaded genes are processed
gene_table = gene_table.loc[~(gene_table.index.isin(downloaded))]
# Group table by species and chromosome
# gene_table.set_index(['species','chromosome'],inplace=True)
# Iterate over species and chromosomes
for sp, chrom in set(gene_table.set_index(['species','chromosome']).index):
    print("{}\t{}".format(sp,chrom))
    # Get dataset for each species
    sp_code = sp[0]+sp.split("_")[-1]
    for sp_dataset in datasets:
        if sp_code in sp_dataset.lower():
            break
    chrom_table = gene_table.loc[(gene_table['species']==sp)&
                                 (gene_table['chromosome']==chrom)]
    ids = chrom_table.index
    id_chunks = [ids[i:i+chunk_size] for i in range(0,len(ids),chunk_size)]
    # Configure search
    for chunk in id_chunks:
        b.new_query()
        b.add_dataset_to_xml(sp_dataset)
        b.add_attribute_to_xml('ensembl_gene_id')
        b.add_attribute_to_xml('peptide')
        ids_str = ','.join(chunk)
        b.add_filter_to_xml('ensembl_gene_id',ids_str)
        xml = b.get_xml()
        xml = xml.replace('virtualSchemaName = "default"','virtualSchemaName = "plants_mart"')
        result = requests.get('http://plants.ensembl.org/biomart/martservice?query='+xml)
        try:
            tab_result = pd.read_csv(io.StringIO(result.text), sep='\t',header=None)
        except:
            print(result.text)
            print(chunk)
        tab_result.columns = ['seq','acc']
        #tab_result.set_index('acc',inplace=True)
        tab_result['length'] = tab_result['seq'].map(lambda x:len(x))
        # If there are more than one isoform with the max length, keep the first one
        tab_result.drop_duplicates(['acc','length'], inplace=True)
        max_isoforms = tab_result.groupby('acc').max()
        with_annotation = pd.concat([max_isoforms,gene_table.loc[max_isoforms.index]],1)
        with_annotation.reset_index(inplace=True)
        selected_data = with_annotation[['species','chromosome','acc','symbol',
                                         'start','end','strand','seq']]
        selected_data = selected_data.astype(str)
        data_string = selected_data.set_index('species').to_records()
        formatted_list = ''
        for data_item in data_string:
            data = list(data_item)
            formatted_list += '>' + '|'.join(data[0:7]) + '\n' + data[7] + '\n'
        fileO.write(formatted_list)
        fileO.flush()