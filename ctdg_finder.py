import argparse
import json
import os
import shutil
import tarfile
from collections import Counter
from concurrent import futures
from copy import copy
from functools import partial
from glob import glob
from time import time

import numpy as np
import pandas as pd
from Bio import SeqIO
from sklearn.cluster import MeanShift
from termcolor import colored

pd.options.mode.chained_assignment = None


# Define class CTDG


class CtdgConfig:
    def __init__(self, evalue, out_dir, db, blast_samples, sp):
        self.evalue = evalue
        self.sp = sp
        self.blast_samples = blast_samples
        self.db = db
        self.out_dir = out_dir.strip('/')
        self.genome_file = "{}/chromosomes.csv".format(self.db)
        self.all_genes_file = "{}/genes_parsed.csv".format(self.db)
        self.all_genes_fasta = "{}/all_seqs.fa".format(self.db)
        self.genomes = None
        self.all_genes = None
        self.blast_rows = None
        self.ms_result = None
        self.family_numbers_list = None
        # self.chrom_wide = []
        self.genome_wide = []

    def check_arguments(self):
        """
        Perform assertion statements to check if all the files needed
        for the analysis are present
        :return: None
        """
        # Check that the directory with the database exists
        assert os.path.exists(self.db), "directory {} with database does not exist".format(self.db)
        db_path = '{}/all_seqs.fa.'.format(self.db)
        # Check that the blast database files exists
        blast_files = sum([os.path.exists(db_path + x) for x in ['phr', 'pin', 'psq']])
        assert blast_files == 3, "Some of the blast database files with prefix all_seqs.fa do not exist"
        # Check that the chromosomes information table exists
        assert os.path.exists(self.genome_file), "Chromosome information file" + \
                                                 "{}/chromosomes.csv does not exist".format(self.genome_file)
        # Check that the annotation table exists
        assert os.path.exists(self.genome_file), "Annotation file" + \
                                                 "{}/genes_parsed.csv does not exist".format(self.genome_file)

        # Check that number of blast samples has been set
        assert self.blast_samples, "Number of blast samples was not specified"
        # Make sure that the output directory is not the directory where the database is or the directory where
        # the CTDGFinder resides
        banned_dirs = [os.getcwd(), self.db, '.']
        assert self.out_dir not in banned_dirs, "output direcrory can't be the ctdgfinder directory" + \
                                                " or the directory where the ctdg database is stored"

    def init_tables(self):
        """
        Initialize genome and gene annotation tables
        :return: None
        """
        self.genomes = pd.read_csv(self.genome_file)
        self.genomes.dropna(inplace=True)
        self.genomes.loc[:, 'chromosome'] = self.genomes['chromosome'].astype(str)
        self.all_genes = pd.read_csv(self.all_genes_file)
        self.all_genes.loc[:, 'chromosome'] = self.all_genes['chromosome'].astype(str)
        self.all_genes = self.all_genes.loc[~(self.all_genes['chromosome'].isnull()) &
                                            (self.all_genes['chromosome'] != "NA")]
        self.all_genes.set_index('acc', inplace=True)


class CtdgRun:
    def __init__(self, db, name_family, ref_sequence):
        self.ref_sequence = ref_sequence
        # Check that the reference sequence exists
        assert os.path.exists(self.ref_sequence), "reference sequence {} des not exist".format(self.ref_sequence)
        self.name_family = name_family
        self.blast_out = "{0}/report/{0}.blast".format(self.name_family)
        # Load database from ctdgDB object
        self.evalue = db.evalue
        self.sp = db.sp
        self.blast_samples = db.blast_samples
        self.db = db.db
        self.out_dir = db.out_dir
        self.genome_file = db.genome_file
        self.all_genes_file = db.all_genes_file
        self.all_genes_fasta = db.all_genes_fasta
        self.genomes = db.genomes
        self.all_genes = db.all_genes
        self.blast_rows = db.blast_rows
        self.ms_result = db.ms_result
        self.family_numbers_list = db.family_numbers_list
        # self.chrom_wide = []
        self.genome_wide = db.genome_wide

    def remove_if_unfinished(self):
        """
        Remove unfinished analysis
        :return: None
        """
        if os.path.exists(self.name_family):
            print("Partial results for {} were found, removing them".format(self.name_family))
            shutil.rmtree(self.name_family)

    def select_species(self):
        """
        Check if the specified species are included in the CTDGFinder database
        and format a list in the class
        :return:
        """
        if len(self.sp) == 0:
            print("Running analysis with all species")
        else:
            valid_species = set(self.genomes['species'])
            valid_species_str = "\n".join(valid_species)

            for spp in self.sp:
                assert spp in valid_species, "\n{} is not a valid species\n".format(spp) + \
                                             "Please select species from the following list:\n{}\n".format(
                                                 valid_species_str)

    def create_folders(self):
        """
        Create the directories where all the
        results will be saved
        """
        if not os.path.exists(self.name_family):
            os.makedirs(self.name_family)
            folders = ['blast_samples', 'blast_samples_gw', 'pre_blast', 'report']
            for sub_dir in folders:
                target = self.name_family + '/' + sub_dir
                if not os.path.exists(target):
                    os.makedirs(target)
        print("Directory tree for {} was created.".format(self.name_family))

    # Analysys
    @staticmethod
    def blast_filter(series):
        """
        This function calculates the length ratio of subject and query in a blast hits table output
        :param series: row of blast output (pd.Series)
        :return: length ratio
        """
        cols = ['s_len', 'q_len']
        ratio = min(series[cols]) / max(series[cols])
        return ratio

    # @staticmethod
    def blast_exe(self, cpus, ref, subject, out_file):
        """
        Blast a reference sequence againsts all the sequences in a blast database
        :param subject: blast database
        :param cpus: number of cpus to be used by blast
        :param ref: Query
        :param out_file: Output for blast
        :return: pd.DataFrame with filtered sequences
        """
        # Set blast command
        cmd = "blastp -query {0} " \
              "-db {1} " \
              "-out {2} " \
              "-evalue {3} " \
              "-outfmt '6 qseqid sseqid qlen slen qstart qend sstart send length gaps gapopen evalue bitscore' " \
              "-num_threads {4}".format(ref, subject, out_file, self.evalue, cpus)
        # Run blast
        print("Running blast with E-value = {}".format(self.evalue))
        os.system(cmd)

    def blast_parse(self, out_file, acc_col, sp_list, tab=True, for_dict=False):
        """
        Parse Blast output, and get annotation from the sequence names
        :param sp_list: optional species list to restrict the analysis
        :param out_file: output
        :param acc_col: sequence name field (separated by |) that contains the accession number
        :param tab: parse annotation from sequence name
        :param for_dict: Analysis for building the json files?
        :return: data frame
        """
        if len(sp_list) > 0:
            print("Parsing blast output for species:")
            print("\n".join(sp_list))
        else:
            print("Running analysis with all species")
        blast_size = os.stat(out_file).st_size / 1000
        print("Blast results size: {} kb".format(blast_size))
        # Read output
        if os.stat(out_file).st_size == 0:
            print("Blast output is empty")
            no_records = pd.DataFrame()
            return no_records
        else:
            result_tab = pd.read_table(out_file, header=None)
            result_tab.columns = ['query', 'subject', 'q_len', 's_len',
                                  'q_start', 'q_end', 's_start', 's_end',
                                  'aln_len', 'gaps', 'gap_open', 'eval', 'bitscore']
            # Sort the results by query, subject and alignment length, so that they can be filtered
            if tab and not for_dict:
                if len(np.unique(result_tab['query'].values)) > 1:
                    # new_query_name = result_tab['query'].values[0]
                    new_query_name = 'multiple_sequences'
                    result_tab.loc[:, 'query'] = new_query_name

            # Filter out hits if they are less than three times (or greater
            #  than three times) the query length
            result_tab.loc[:, 'len_ratio'] = result_tab.apply(self.blast_filter, axis=1)
            result_tab.sort_values(by=['query', 'subject', 'len_ratio'], inplace=True, ascending=True)
            result_tab.drop_duplicates(subset=['query', 'subject'], keep='last', inplace=True)

            filtered_blast = result_tab.loc[result_tab.len_ratio >= 0.3]
            # Save filtered output if run is for family definition
            filtered_blast.to_csv("{}_filtered".format(out_file))
            # If more than one query was used, rename all the queries with the same name
            if tab:
                try:
                    filtered_blast.loc[:, 'query_acc'] = filtered_blast['query'].map(lambda x: x.split("|")[acc_col])
                except IndexError:
                    filtered_blast.loc[:, 'query_acc'] = filtered_blast['query'].map(lambda x: x.split("|")[0])
                # Attach the query name and the evalue to the subject, to include them as columns
                filtered_blast.loc[:, 'subject'] = filtered_blast.query_acc + '|' + filtered_blast.subject + \
                                                   "|" + filtered_blast['eval'].astype(str)
                # Extract table from subject names

                def get_fields(fields):
                    """
                    Parse the name of a gene correctly in case there are | in the gene symbol
                    :param row:
                    :return: list of parsed fields
                    """
                    if len(fields) == 10:
                        pre_name = fields[:4]
                        name = ['|'.join(fields[4:6])]
                        post_name = fields[6:]
                        fields = pre_name + name + post_name
                    return fields

                temp_fields = open("{}._fields.temp".format(out_file), "w")
                temp_fields.write("query,species,chromosome,prot_acc,symbol,start,end,strand,evalue\n")

                for line in filtered_blast.subject.values:
                    temp_fields.write(','.join(get_fields(line.split("|"))) + '\n')
                temp_fields.close()
                del filtered_blast
                # Set table
                sub_table = pd.read_csv("{}._fields.temp".format(out_file))
                os.remove("{}._fields.temp".format(out_file))
                # sub_table = pd.DataFrame(list(fields_list), columns=['query', 'species', 'chromosome', 'prot_acc',
                #                                                 'symbol', 'start', 'end', 'strand'])
                # In case there are spaces in species names, remove them
                sub_table.loc[:, 'species'] = sub_table['species'].map(lambda x: str(x.replace(" ", "_")))
                # Only consider hits from selected species
                if len(sp_list) > 0:
                    sub_table = sub_table.loc[sub_table['species'].isin(sp_list)]
                sub_table.loc[:, 'start'] = sub_table['start'].astype(int)
                sub_table.loc[:, 'end'] = sub_table['end'].astype(int)
                sub_table.loc[:, 'strand'] = sub_table['strand'].astype(int)
                sub_table.loc[:, 'Length'] = abs(sub_table['end'] - sub_table['start'])
                sub_table.loc[:, 'chromosome'] = sub_table['chromosome'].astype(str)
                sub_table.sort_values(by=['species', 'chromosome', 'start'], inplace=True)
                # Remove hits in non-assembled chromosomes (almost deprecated)
                # sub_table = sub_table.loc[sub_table['chromosome'] != 'NA']
                sub_table.to_csv("{}_out".format(out_file))
                if not sub_table.shape[0] > 0:
                    print("Not enough blast hits. Terminating")
                    return None
                # Return filtered blast results
                return sub_table
            else:
                return filtered_blast

    @staticmethod
    def sp_loop(in_table, columns):
        """
        Filters a table by a number of columns
        :param in_table: Input table
        :param columns: Column names to perform the filter
        :return: list of tables for each combination of column names
        """
        # Initialize the list of tables
        list_results = []
        for col_set in set(in_table.set_index(columns).index):
            filtered_table = in_table
            cols_to_filter = {col_name: col_value for col_name, col_value in zip(columns, col_set)}
            for var in cols_to_filter.keys():
                filtered_table = filtered_table.loc[filtered_table[var] == cols_to_filter[var]]
            list_results.append(filtered_table)
        return list_results

    @property
    def ms_compiled(self) -> pd.DataFrame:
        """
        Assemble meanshift table from meanshift analysis
        :ms_result: map object from meanshift analysis
        :return: organized table
        """
        return pd.concat(self.ms_result)

    @property
    def only_clusters_rows(self):
        """
        Remove clusters that did not pass the statisti
        :return:
        """
        ms_compiled_temp = self.ms_compiled
        table = ms_compiled_temp.loc[~(ms_compiled_temp['cluster'].isin(["na_ms", "na_95",  "0", "0.0", 0, 0.0]))]
        only_rows = [sptable for sptable in self.sp_loop(table, ["species", "chromosome", "cluster"])]
        return only_rows

    @property
    def family_numbers(self):
        # Quit if clusters were not found
        family_list_filtered = [x for x in self.family_numbers_list if x is not None]
        if len(family_list_filtered) == 0:
            self.delete_intermediates()
            print("NO CLUSTERS in the {} family".format(self.name_family))
            return None
        else:
            return pd.DataFrame(family_list_filtered,
                                columns=['species', 'chromosome', 'cluster', 'start', 'end',
                                         'duplicates', 'total_genes', 'proportion_duplicates'])

    @property
    def cluster_rows(self):
        if self.family_numbers is None:
            return None
        else:
            return self.sp_loop(self.family_numbers, ['species', 'chromosome', 'cluster'])

    def add_sample_cols(self, table):
        # for chrom_data, genome_data in zip(self.chrom_wide, self.genome_wide):
        for genome_data in self.genome_wide:
            # table.loc[(table.species == chrom_data[0]) &
            #       (table.cluster == chrom_data[1]), 'perc95_chrom'] = chrom_data[2]

            table.loc[(table.species == genome_data[0]) &
                      (table.cluster == genome_data[1]), 'perc95_gw'] = genome_data[2]
        return table

    def add_samples(self, new_numbers):
        ms_compiled = self.ms_compiled
        # Identify clusters that didn't make it through the threshold
        for_deletion = new_numbers.loc[new_numbers['duplicates'] < new_numbers['perc95_gw'],
                                       ['species', 'chromosome', 'cluster']]
        # Get accession numbers for the genes that are going to be re annotated
        accs_for_deletion = pd.merge(ms_compiled, for_deletion, how='inner',
                                     on=['species', 'chromosome', 'cluster'])['prot_acc'].values
        # Remove annotation for cluster name and cluster order
        ms_compiled.loc[ms_compiled['prot_acc'].isin(accs_for_deletion), ['cluster', 'order']] = 'na_95'
        ms_compiled.set_index("query", inplace=True)
        ms_compiled.to_csv("{0}/report/{0}_genes.csv".format(self.name_family))
        new_numbers.loc[new_numbers['duplicates'] < new_numbers['perc95_gw'], 'cluster'] = 'na_95'
        new_numbers.set_index("species", inplace=True)

        # # # Save cluster tables
        new_numbers.to_csv("{0}/report/{0}_numbers.csv".format(self.name_family))
        new_numbers.loc[~(new_numbers['cluster'].isin(['na_95', 'na_ms', '0', 0]))].to_csv(
            "{0}/report/{0}_numbers_clean.csv".format(self.name_family))

    def delete_intermediates(self):
        """
        Remove intermediate files from blast sampling
        :return: None
        """
        # Move files from the query enrichment step to its directory if
        # the -i flag was turned on
        if args.iterative:
            for pre_blast_file in glob("{}/pre*.*".format(self.name_family)) + \
                    ["{0}/{0}_ext_query.fa".format(self.name_family)]:
                filename = pre_blast_file.split("/")[-1]
                shutil.move(pre_blast_file, "{}/pre_blast/{}".format(self.name_family, filename))
        sample_tar_o = tarfile.open('{}/report/blast_samples.tgz'.format(self.name_family), 'w:gz')
        for sample_folder in ['blast_samples', 'blast_samples_gw']:
            # Process fastas
            for x in glob("{}/{}/*.fa".format(self.name_family, sample_folder)):
                os.remove(x)
            # Process coordinates used for the sampling step
            for coord in glob("{}/{}/*.coords".format(self.name_family, sample_folder)):
                sample_tar_o.add(coord, recursive=False)
                os.remove(coord)
            # Save blast outputs in tarfile, and remove the files
            for blast_file in glob("{}/{}/*.blast*".format(self.name_family, sample_folder)):
                sample_tar_o.add(blast_file, recursive=False)
                os.remove(blast_file + "_filtered")
                os.remove(blast_file)
            for sample in glob("{}/{}/*.samples".format(self.name_family, sample_folder)):
                os.remove(sample)
        sample_tar_o.close()
        for folder, _, __ in os.walk(self.name_family):
            if len(os.listdir(folder)) == 0:
                os.removedirs(folder)
        # Move result to output directory
        os.makedirs(self.out_dir, exist_ok=True)
        shutil.move(self.name_family, '{}/{}'.format(self.out_dir, self.name_family))

    @staticmethod
    def set_sample_tables(tabs, dataset):
        sample_cols = {-1: 'species', 0: 'cluster'}
        tabs = tabs.rename(columns=lambda x: x - 1 if type(x) != int else x)
        tabs = tabs.rename(columns=sample_cols)
        tabs.loc[:, 'dataset'] = dataset
        return tabs






# These functions were placed outside the class because if they were inside, the whole class
# would have been copied for each processor

def pre_blast(cpu, ref, all_genes, name_family, sp):
    print("Running blast search step to extend query")
    CTDG.blast_exe(cpu, ref, all_genes, "{0}/pre_{0}.blast".format(name_family))
    pre_family_blast = CTDG.blast_parse("{0}/pre_{0}.blast".format(name_family), acc_col=2, tab=True,
                                       sp_list=sp, for_dict=False)
    hits = pre_family_blast['prot_acc'].values
    with open("{0}/{0}_ext_query.fa".format(name_family), "w") as fileO:
        for seq in SeqIO.parse(CTDG.all_genes_fasta, 'fasta'):
            if seq.name.split("|")[2] in hits:
                SeqIO.write(seq, fileO, "fasta")


def meanshift_cluster(ms_sp_table, CTDG):
    """
        Extract clusters from annotation using the mean shift algorithm
        :param ms_sp_table: blast hits table for each species/chromosome
        :return: annotated blast table
        """
    # Set variables
    sp_mean_shift = ms_sp_table['species'].values[0]
    chrom_mean_shift = str(ms_sp_table['chromosome'].values[0])
    proteome = CTDG.all_genes.loc[(CTDG.all_genes['species'] == sp_mean_shift) &
                                 (CTDG.all_genes['chromosome'] == chrom_mean_shift)]
    proteome.sort_values("start",inplace=True)
    # If there is only one gene, set the cluster to zero -> single-copy gene in the chromosome
    if len(ms_sp_table) == 1:
        ms_sp_table.loc[:, 'cluster'] = 0
        ms_sp_table.loc[:, 'order'] = 0
    else:
        # Calculate the mean between neighboring genes in the chromosome
        gene_distances = proteome['start'].values[1:] - proteome['end'][:-1]
        mean_distance = np.mean(gene_distances)
        # Calculate standard deviation of the distance between neighboring genes in the chromosome
        sd_distance = np.std(gene_distances)
        # The band_width in MeanShift is set to the mean intergenic distance plus its standard deviation
        # This is more or less the minumum distance between "true" neighboring members of a cluster
        band_width = mean_distance + sd_distance
        # Since MeanShift works on 2D data, a 'y' axis was generated using zeroes
        gene_starts = np.array([(x, y) for x, y in zip(ms_sp_table.start.values.astype(int),
                                                       np.zeros(len(ms_sp_table)))])
        # Set the MeanShift
        ms = MeanShift(bandwidth=band_width)
        # Execute the MeanShift algorithm to the coordinates
        try:
            ms.fit(gene_starts)
        except ValueError:
            print(band_width, sp_mean_shift, chrom_mean_shift, proteome.shape)
        # The labels are the position of the members of each cluster in the coordinates array
        labels = ms.labels_
        labels_unique = np.unique(labels)
        # Number of clusters
        n_clusters = len(labels_unique)
        # Start from cluster 1
        cluster_number = 1
        # Go through each cluster label
        for k in range(n_clusters):
            # Set a masking array with the elements that belong to each cluster
            members = labels == k
            # Assign members of the cluster to new array
            proto_cluster = gene_starts[members, 0]
            cluster = np.sort(proto_cluster)
            # If there is more than one gene in the cluster
            if len(proto_cluster) > 1:
                # Annotate name (<name of the family>-<chromosome>_consecutive number
                ms_sp_table.loc[ms_sp_table['start'].isin(cluster),
                                'cluster'] = "{}-{}_{}".format(CTDG.name_family, chrom_mean_shift, cluster_number)
                # Annotate order of genes "from start of the chromosome, according to annotation
                ms_sp_table.loc[ms_sp_table['start'].isin(cluster),
                                'order'] = range(1, len(cluster) + 1)
                cluster_number += 1
            else:
                # If there is only one gene in the "cluster", assign name and order to zero
                ms_sp_table.loc[ms_sp_table['start'].isin(cluster),
                                'cluster'] = 'na_ms'
                ms_sp_table.loc[ms_sp_table['start'].isin(cluster),
                                'order'] = 'na_ms'
    # Return annotated table per species (useful for parallelization)
    return ms_sp_table


def gene_numbers(sp_table, all_genes_tab):
    """
    Count the number of duplicates, total number of genes and proportion of duplicates
    for each cluster
    :param all_genes_tab: complete gene annotation
    :param sp_table: blast table
    :return: annotated numbers table
    """
    sp_numbers = sp_table['species'].values[0]
    chromosome = sp_table['chromosome'].values[0]
    cluster = sp_table['cluster'].values[0]
    duplicates_genes = len(sp_table)
    # If there is more than one duplicates:
    if duplicates_genes > 1:
        start = sp_table['start'].min()
        end = sp_table['end'].max()
        # Count the genes in that chromosomal region
        total_genes = len(all_genes_tab.loc[(all_genes_tab['species'] == sp_numbers) &
                                            (all_genes_tab['chromosome'] == chromosome) &
                                            (all_genes_tab['start'] >= start) &
                                            (all_genes_tab['end'] <= end)])
        # Calculate proportion of duplicates
        proportion_duplicates = duplicates_genes / total_genes
        # Return the results per cluster (useful for parallelization
        gene_numbers_list = [sp_numbers, chromosome, cluster, start, end,
                             duplicates_genes, total_genes, proportion_duplicates]
        return gene_numbers_list
    else:
        # If there are no genes in the cluster, return None
        return None


def grab_duplicates(acc_list, duplicates, evalue):
    """
    Given a list of accession numbers, get the maximum number of duplicates between them
    :param evalue: reference E-value
    :param acc_list: accession numbers list
    :param duplicates: Dictionary with parsed blast results for the chromosome being sampled
    :return: max number of duplicates
    """
    duplicates_list = []
    # print(len(duplicates))
    # Retain only blast hits less than or equal to a given E-value
    duplicates_evalue = {}
    for acc in duplicates:
        duplicates_eval_list = []
        for hit in duplicates[acc]:
            if hit[1] <= evalue:
                duplicates_eval_list.append(hit[0])
        duplicates_evalue[acc] = duplicates_eval_list
        # eval_removed = len(duplicates[acc]) - len(duplicates_evalue[acc])
        # if eval_removed > 0:
        #    print("{}: Paralogs removed by E-value {}: {}".format(acc, evalue, eval_removed))
    # duplicates = json.loads(open('{}/{}_{}.json'.format(blast_dir, sp.replace(' ', '_'), chrom)).read())
    # If there are no genes in the sample interval, return 0 duplicates
    if len(acc_list) == 0:
        duplicates_max = 0
    # If there are more than one, return the maximum number of duplicates
    elif type(acc_list) == list:
        for acc in acc_list:
            # If the accession is not in the query, it had no blast hits with any sequence
            if acc in duplicates_evalue.keys():
                duplicates_list.extend(list(set(duplicates_evalue[acc]).intersection(acc_list)))
        # If more than zero duplicates were found
        if len(duplicates_list) > 0:
            duplicates_max = Counter(duplicates_list).most_common(1)[0][1]
        else:
            duplicates_max = 0
    else:
        duplicates_max = Counter(duplicates_evalue[acc_list]).most_common(1)[0][1]
    return duplicates_max


def blast_sampling(pre_cluster_table, gw, db, name_family, blast_samples, genomes, all_genes_blast, evalue, CTDG):
    sp = pre_cluster_table.loc[:, 'species'].values[0]
    # Load blast hits with queries from the species
    #sp_duplicates = json.loads(open("{}/blasts/{}.json".format(db, sp)).read())
    cluster = pre_cluster_table.loc[:, 'cluster'].values[0]
    cluster_length = abs(pre_cluster_table.loc[:, 'end'].values[0] - pre_cluster_table.loc[:, 'start'].values[0])
    # Set output directory depending of the type of sampling
    if gw:
        root_folder = '{}/blast_samples_gw'.format(name_family)
        msg = 'G'
        col_name = 'perc95_gw'
    else:
        root_folder = '{}/blast_samples'.format(name_family)
        msg = 'C'
        col_name = 'perc95_chrom'
    file_name = "{}/{}_{}".format(root_folder, sp.replace(' ', '_'), cluster)
    sample_coords_file = open("{}/{}_{}.coords".format(root_folder, sp.replace(' ', '_'), cluster), 'w')
    # with open(file_name, 'w') as seqO:
    sample_coords = []
    max_paras_list = []
    for i in range(1, blast_samples + 1):
        if gw:
            chromosome = np.random.choice(CTDG.genomes.loc[genomes['species'] == sp, 'chromosome'].values)
        else:
            chromosome = pre_cluster_table.loc[:, 'chromosome'].values[0]

        chr_len = genomes.loc[(genomes['species'] == sp) &
                              (genomes.chromosome == chromosome), 'length'].values[0]
        # Set sample coordinates
        random_start = np.random.randint(1, chr_len)
        random_end = random_start + cluster_length

        # Save sample coordinates in log file
        sample_coords_file.write(','.join([str(i), str(sp), str(chromosome),
                                           str(random_start), str(random_end)]) + '\n')
        # Get genes in sample regions
        proteome_slice = all_genes_blast.loc[(all_genes_blast.species == sp) &
                                             (all_genes_blast.chromosome == chromosome) &
                                             (all_genes_blast.start >= random_start) &
                                             (all_genes_blast.end <= random_end)].index.values
        if len(proteome_slice) > 0:
            chrom_duplicates = json.loads(open("{}/blasts/{}_{}.json".format(db, sp, chromosome)).read())
            max_duplicates = grab_duplicates(list(proteome_slice), chrom_duplicates, evalue)
            current_sample = [i, sp, chromosome, random_start, random_end, max_duplicates]
            sample_coords.append(current_sample)
        else:
            max_duplicates = 0
        max_paras_list.append(max_duplicates)
    duplicates_log = open(file_name + ".samples", 'w')
    duplicates_data = ','.join([str(x) for x in max_paras_list])
    duplicates_log.write("{},{},{}\n".format(sp, cluster, duplicates_data))
    duplicates_log.close()
    try:
        original_duplicates = pre_cluster_table['duplicates'].values[0]
    except KeyError:
        print("Error in :{}, {}".format(sp, pre_cluster_table.cluster))
        return pre_cluster_table
    with_perc = pre_cluster_table.loc[:]
    with_perc.loc[:, col_name] = np.percentile(max_paras_list, 95)
    query_perc = (sp, cluster, np.percentile(max_paras_list, 95))
    max_sp_len = max([len(x) for x in set(genomes['species'])]) + 2
    status_msg = "{:<{sp_len}} {:<10} {:<30} {} ({})".format(sp, original_duplicates,
                                                            cluster, msg, round(query_perc[2], 3), sp_len=max_sp_len)
    if gw and original_duplicates >= query_perc[2]:
        print(colored(status_msg, 'green'))
    elif gw and original_duplicates < query_perc[2]:
        print(colored(status_msg, 'red'))
    else:
        print(status_msg)
    return query_perc


###
if __name__ == "__main__":
    # Define arguments
    parser = argparse.ArgumentParser("Paralogs cluster annotation pipeline")
    parser.add_argument("--name_family", "-n",
                        action='store',
                        help="Name of the gene family")

    parser.add_argument("--ref_seq", "-r",
                        action='store',
                        help="reference gene family sequence")

    parser.add_argument("--blast_samples", "-b",
                        action="store",
                        type=int,
                        help="Number of samples to build empirical distribution of duplicates")
    parser.add_argument("--sp", "-s",
                        action="append",
                        default=[],
                        help="Species to run the analysis on (must be written in quotes)")
    parser.add_argument("--out_dir", "-o",
                        action="store",
                        default='CTDG_out',
                        help="Output directory")
    parser.add_argument("--cpu", "-c",
                        action="store",
                        type=int,
                        default=1,
                        help="CPU to be used")
    parser.add_argument("--db", "-d",
                        action="store",
                        help="Directory with gene annotation and blast database")
    parser.add_argument("--evalue", "-e",
                        action="store",
                        type=float,
                        default=1e-3,
                        help="E-value threshold for blast steps (default: 1e-3)")
    parser.add_argument("--iterative", "-i",
                        action="store_true",
                        help="Perform an extra blast search at the beginning to enrich the query set")
    parser.add_argument("--dir", "-D",
                        action="store",
                        default=None,
                        help="run analyses with all the sequences in a directory")
    # Check if blast is installed. Since this is not required for defining the analysis, it is executed before
    # the class definition
    blast_path = shutil.which('blastp')
    assert bool(blast_path), "blastp is not installed or it is not in your PATH"

    # Parse arguments
    args = parser.parse_args()
    ctdg_config = CtdgConfig(out_dir=args.out_dir, db=args.db,
                             blast_samples=args.blast_samples, sp=args.sp, evalue=args.evalue)
    ctdg_config.check_arguments()
    ctdg_config.init_tables()

    def run(args):
        CTDG = CtdgRun(ctdg_config, name_family=args.name_family, ref_sequence=args.ref_seq)
        CTDG.remove_if_unfinished()
        if os.path.exists("{}/{}".format(CTDG.out_dir, CTDG.name_family)):
            print("Results for {} are already saved in {}".format(CTDG.name_family, CTDG.out_dir))
            return None
        # Load tables
        # Set selected species
        CTDG.select_species()
        # Start chronometer
        init_time = time()
        # Create directory structure
        CTDG.create_folders()
        if args.iterative:
            # Run the blast step and use the resulting hits as new query
            pre_blast(args.cpu, CTDG.ref_sequence, CTDG.all_genes_fasta, CTDG.name_family, CTDG.sp)
            # Run blast
            CTDG.blast_exe(args.cpu, "{0}/{0}_ext_query.fa".format(CTDG.name_family), CTDG.all_genes_fasta, CTDG.blast_out)
        else:
            CTDG.blast_exe(args.cpu, CTDG.ref_sequence, CTDG.all_genes_fasta, CTDG.blast_out)
        # Parse blast result

        CTDG.family_blast = CTDG.blast_parse(CTDG.blast_out, acc_col=2, tab=True,
                                             sp_list=CTDG.sp, for_dict=False)
        # End the run if there are not enough blasts
        if CTDG.family_blast is None:
            # CTDG.delete_intermediates()
            return None

        if CTDG.family_blast.shape[0] == 0:
            # CTDG.delete_intermediates()
            raise Exception("Blast output is empty")
        # Run MeanShift in parallel
        family_blast = copy(CTDG.family_blast)
        p_meanshift_cluster = partial(meanshift_cluster, CTDG=CTDG)
        with futures.ProcessPoolExecutor(args.cpu) as p:
            ms_result = p.map(p_meanshift_cluster, CTDG.sp_loop(family_blast, ["species", "chromosome"]))
        CTDG.ms_result = list(ms_result)
        # Get number of duplicates, genes and proportions of duplicates per cluster
        # only_clusters_rows = copy(CTDG.only_clusters_rows)
        all_genes = copy(CTDG.all_genes)
        partial_gene_numbers = partial(gene_numbers, all_genes_tab=all_genes)

        with futures.ProcessPoolExecutor(args.cpu) as p:
            family_numbers_list = p.map(partial_gene_numbers, CTDG.only_clusters_rows)
        CTDG.family_numbers_list = list(family_numbers_list)

        one_arg_blast_samples_gw = partial(blast_sampling, gw=True, db=CTDG.db, name_family=CTDG.name_family,
                                           blast_samples=CTDG.blast_samples, genomes=CTDG.genomes,
                                           all_genes_blast=CTDG.all_genes, evalue=CTDG.evalue,
                                           CTDG=CTDG)
        # Run the sampling algorithm
        max_sp_len = max([len(x) for x in set(CTDG.genomes['species'])]) + 2
        if CTDG.cluster_rows is None:
            # CTDG.delete_intermediates()
            return None
        print("Analyzing {} proto-cluster(s)".format(len(CTDG.cluster_rows)))
        print("{:<{sp_len}} {:<10} {:<30} {}".format('species', 'duplicates',
                                                    'proto-cluster', 'sample (95P)', sp_len=max_sp_len))

        cluster_rows = copy(CTDG.cluster_rows)
        with futures.ProcessPoolExecutor(args.cpu) as p:
            # chrom_wide = p.map(one_arg_blast_samples, cluster_rows)
            genome_wide = p.map(one_arg_blast_samples_gw, cluster_rows)
        # CTDG.chrom_wide, CTDG.genome_wide = list(chrom_wide), list(genome_wide)
        CTDG.genome_wide = list(genome_wide)
        # CTDG.genome_wide = map(one_arg_blast_samples_gw, cluster_rows)
        new_tab = CTDG.add_sample_cols(CTDG.family_numbers)
        CTDG.add_samples(new_tab)
        CTDG.delete_intermediates()

        # Finish
        print("DONE")
        run_time = time() - init_time
        seconds = int(run_time % 60)
        minutes = int(run_time / 60)
        hours = int(minutes / 60)
        minutes -= hours * 60
        print("Results for {} were saved in {}".format(CTDG.name_family, CTDG.out_dir))
        print("Run time: {}:{}:{}\n".format(str(hours).zfill(2), str(minutes).zfill(2), str(seconds).zfill(2)))

    if args.dir:
        running = 0
        ref_files = glob("{}/*".format(args.dir))
        for file in ref_files:
            running += 1
            args.ref_seq = file
            args.name_family = file.split("/")[-1].split(".")[0]
            if os.path.isdir("{}/{}".format(args.out_dir, args.name_family)):
                print("{} was already run".format(args.name_family))
            else:
                print("Running analysis {} of {}".format(running, len(ref_files)))
                run(args)
    else:
        run(args)
