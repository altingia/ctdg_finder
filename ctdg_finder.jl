using ZipFile
using ArgParse
using Base.Test
using DataFrames
using Bio.Seq
using PyCall
using Formatting
@pyimport sklearn.cluster as cl
"""
Parse script options
"""
function parse_commandline()
  s = ArgParseSettings()
  @add_arg_table s begin
    "--name_family", "-n"
      help = "Name of gene family"
      required = false
    "--ref_seq", "-r"
      help = "Reference sequence (query)"
      required = false
    "--blast_samples", "-b"
      help = "Number of samples to build the empirical distribution"
      arg_type = Int
      default = 1000
    "--sp", "-s"
      help = "Restrict the analysis to a species set (optional)"
      nargs = '*'
      action = :append_arg
    "--out_dir", "-o"
      help = "Output directory"
      default = "CTDG_out"
    "--cpu", "-c"
      help = "Number of CPUs to use"
      arg_type = Int
      default = 6
    "--db", "-d"
      help = "Database to use"
      required = true
    "--evalue", "-e"
      help = "E-value threshold"
      arg_type = Float64
      default = 1e-3
     "--dir", "-D"
      help = "Batch analysis"
      default = ""
   "--iterative", "-i"
      help = "Iterative mode"
      action = :store_true
  end
  return parse_args(s)
end

# Functions
"""
Test if `blastp` exists in PATH
"""
function test_blast()
  # return (./blastp, true)
  blast_exist = false
  blast_path = "None"
  for path = split(ENV["PATH"],":")
    blast_path = path * "/blastp"
    if ispath(blast_path)
      blast_exist = true
      break
    end
  end
  return (blast_path, blast_exist)
end

"""
Checks that the CTDG database contains the correct files
"""
function check_db(dir)
  int2str = x -> string(x)
  @assert ispath(dir) "Directory with CTDG database does not exist"
  db_files = readdir(dir)
  for i in ["all_seqs.fa", "all_seqs.fa.phr", "all_seqs.fa.pin",
            "all_seqs.fa.psq", "chromosomes.csv","genes_parsed.csv", "blasts"]
    @assert i in db_files "$i does not exist in $dir"
  end
  genomes = readtable(dir * "/chromosomes.csv")
  genes = readtable(dir * "/genes_parsed.csv")
  # Only use columns of interest
  genomes = genomes[:, [:species, :chromosome, :length, :bandwidth]]
  genes = genes[:, [:acc, :chromosome, :species, :symbol,
		    :start, :_end, :strand, :length]]
  genes[:, :strand] = map(int2str, genes[:, :strand])
  genes[:, :chromosome] = map(int2str, genes[:, :chromosome])
  return (genes, genomes)
end

"""
Remove unfinished analysis
"""
function remove_if_incomplete(args)
  if ispath(args["name_family"])
    println("Partial results found for $(args["name_family"]). Removing them")
    Base.rm(args["name_family"], recursive=true)
  end
end

"""
Check that the selected species (if any) exist in the database
"""
function check_sp(args)
  if ! isempty(args["sp"])
    valid_species = unique(genomes[:species])
    for spp=args["sp"]
      @assert spp in valid_species "$spp is not a valid species"
    end
    println("Running the analysis with the species:")
    println(join(args["sp"], "\n"))
  end
end

"""
Create analysis directory structure
"""
function create_folders(name_family)
  for folder=["/intermediates", "/report"]
    mkpath(name_family * folder)
  end
end

"""
Execute blast search
"""
function blast_exe(blast_path;cpus=1, ref=None,
  subject=None, out_file=None, evalue=None)
  fmt1 = "6 qseqid sseqid qlen slen qstart qend sstart send length "
  fmt2 = "gaps gapopen evalue bitscore"
  fmt = fmt1 * fmt2
  println("Running blast with E-value = $evalue")
  run(`$blast_path -db $subject -query $ref -out $out_file
  -evalue $evalue  -outfmt $fmt -num_threads $cpus`)
end
"""
Calculates the length overlapping ratio
Input: dataframe with columns `q_len` and `s_len`
output: array
"""
function blast_filter(blast)
  l_ratio = Array(Float32, size(blast, 1))
  for (ix, row) in enumerate(eachrow(blast))
        l_ratio[ix] = min(row[:q_len],row[:s_len]) / max(row[:q_len],row[:s_len])
  end
  blast[:len_ratio] = l_ratio
  return blast
end

"""
Parse the name of a gene in case it contains "|" in its name
"""
function get_fields(fields)
  fields_list = split(fields, "|")
  if length(fields_list) == 10
    pre_name = fields_list[1:4]
    name = join(fields_list[5:6], "|")
    post_name = fields_list[7:end]
    fields_list = cat(1, pre_name, name, post_name)
  end
  return fields_list
end

"""
Parse blast output and generate hits table
"""
function blast_parse(out_file; acc_col=3, sp_list=[],
  tab=true, for_dict=false)
  # Use species list if necessary
  if ! isempty(sp_list)
    sp_str = join(sp_list, ", ")
    println("Parsing blast for the following species:\n$sp_str")
  else
    println("Parsing blast for all species")
  end
  # Information about blast size
  blast_size = filesize(out_file) / 1000
  println("Blast results size: $blast_size Kb")
  println("Running $(args["blast_samples"]) samples")
  if blast_size == 0
    println("Blast output is empty")
    no_records = DataFrame()
    return no_records
  else
    # Read table
    result_tab = readtable(out_file, separator=' ', header=false,
    names=[:query, :subject, :q_len, :s_len, :q_start, :q_end, :s_start,
    :s_end, :aln_len, :gaps, :gap_open, :eval, :bitscore])
  end
  # Sort values by query, subject and alignment length
  if tab && ! for_dict
    if length(unique(result_tab[:, :query])) > 1
      result_tab[:, :query] = "multiple_sequences"
    end
  end
  result_tab = blast_filter(result_tab)
  sort!(result_tab, cols = (:query, :subject, :len_ratio),
        rev=(true, true, true))
  filtered_blast = result_tab[result_tab[:len_ratio] .>= 0.3, :]
  unique!(filtered_blast, [:subject])
  filtered_out = "$out_file" * "_filtered"
  writetable(filtered_out, filtered_blast, separator=',')
  queries = filtered_blast[:query]
  if tab
    if length(split(queries[1], '|')) >= acc_col
      filtered_blast[:, :query_acc] = map(x -> split(x, '|')[acc_col], queries)
    else
      filtered_blast[:, :query_acc] = map(x -> split(x, '|')[1], queries)
    end

    sub_ject = map((x,y,z)-> x * "|" * y * "|" * string(z),
    filtered_blast[:, :query_acc],
    filtered_blast[:, :subject],
    filtered_blast[:,:eval])
    fields = map(get_fields, sub_ject)
    str2int(x) = parse(Int, x)
    sub_table = DataFrame()
    sub_table[:, :query] = [string(x[1]) for x in fields]
    sub_table[:, :species] = [string(x[2]) for x in fields]
    sub_table[:, :chromosome] = [string(x[3]) for x in fields]
    sub_table[:, :prot_acc] = [string(x[4]) for x in fields]
    sub_table[:, :symbol] = [string(x[5]) for x in fields]
    sub_table[:, :start] = map(str2int, [x[6] for x in fields])
    sub_table[:, :end] = map(str2int, [x[7] for x in fields])
    sub_table[:, :strand] = [string(x[8]) for x in fields]
    sub_table[:, :evalue] = float([string(x[9]) for x in fields])
    out_file = "$out_file" * "_out"
    writetable(out_file, sub_table, separator=',')
    @assert nrow(sub_table) > 0 "Not enough blast hits. Terminating"
    return sub_table
  else
    return filtered_blast
  end
end


"""
Parse the database sequence as a dictionary of Seq.AminoAcidSequence
"""
function parse_seq_db(db)
  seq_file = "$db/all_seqs.fa"
  seq_dict = Dict()
  open(seq_file) do f
    while !eof(f)
      line = chomp(readline(f))
      if line[1] == '>'
        seq_name = split(line[2:end], '|')[3]
        seq_dict[seq_name] = Seq.AminoAcidSequence()
      else
        seq_dict[seq_name] = seq_dict[seq_name] * Seq.AminoAcidSequence(line)
      end
    end
  end
  return seq_dict
end


"""
If run in iterative mode `iterative`, run blast, gather sequences from
results, and change the query to the new sequence file
"""
function pre_blast(args)
  println("Running blast search step to extend query (--iterative)")
  blast_out_file = format("{1}/intermediates/pre_{1}.blast",args["name_family"])
  blast_exe(blast_path,
  cpus=args["cpu"],
  ref=args["ref_seq"],
  subject=args["db"] * "/all_seqs.fa",
  out_file= blast_out_file,
  evalue=args["evalue"])

  pre_family_blast = blast_parse(blast_out_file, sp_list=args["sp"])
  seq_dict = parse_seq_db(args["db"])
  name_family = args["name_family"]
  new_query = format("{1}/intermediates/{1}_ext_query.fa", name_family)
  open(new_query, "w") do f
    for seq=pre_family_blast[:prot_acc]
      seq_str = string(seq_dict[seq])
      write(f, ">$(seq)\n$(seq_str)\n")
    end
  end
  args["ref_seq"] = new_query
end

"""
Run the meanshift algorithm (prom Python scikit-learn) and annotate
relevant cluster candidates
"""
function MeanShift(blast_table, all_genes, name_family)
  blast_table[:, :cluster] = "0"
  blast_table[:, :order] = 0
  sort!(blast_table, cols=[:species, :chromosome, :start])
  for ms_sp_table=groupby(blast_table, [:species, :chromosome])
    sp_mean_shift = ms_sp_table[1, :species]
    chrom_mean_shift = ms_sp_table[1, :chromosome]
    # println(format("{},{}", sp_mean_shift, chrom_mean_shift))
    # proteome = all_genes[(all_genes[:species] .== sp_mean_shift)&
    # (all_genes[:chromosome] .== chrom_mean_shift), :]
    # sort!(proteome, cols = (:start))
    if nrow(ms_sp_table) > 1
      bandwidth = genomes[(genomes[:species].==sp_mean_shift)&
                   (genomes[:chromosome].==chrom_mean_shift), :bandwidth][1]
      gene_starts = ms_sp_table[:, :start]
      gene_coords = [(x, y) for (x, y) in zip(gene_starts,
                                              zeros(Int, length(gene_starts)))]
      # pre_distances = proteome[2:end, :start] - proteome[1:end-1, :_end]
      # # If there are overlapping genes in different orientations, it might
      # #result in a negative value, for that reason, in cases where negative
      # # distances are obtained, calculate the distance using both start coords
      # gene_distances = pre_distances[pre_distances .> 0]
      # for i = find(pre_distances .<0)
      #   dist = proteome[i+1,:start] + proteome[i, :start]
      #   push!(gene_distances, dist)
      # end
      # If there are only two proteins in the scaffold, use only the
      # mean for estimating the bandwidth
      # if nrow(proteome) == 2
	    #   bandwidth = abs(gene_distances[1])
      # else
	    #   bandwidth = mean(gene_distances) + std(gene_distances)
      # end

      ms = cl.MeanShift(bandwidth=bandwidth)
      #println(format("sp: {}\nchromosome:{}\ngenes in chromosome: {}\nbandwidth: {}\ngene coords:\n{}\n",
	#	     sp_mean_shift, chrom_mean_shift, nrow(proteome),bandwidth, join(gene_coords,"\n")))

      group_labels = ms[:fit_predict](gene_coords)

      ms_sp_table[:, :cluster] = map((x,y) -> "$(name_family)_" * string(x) * "_" * string(y),
      ms_sp_table[:chromosome], group_labels)
      for group in groupby(ms_sp_table, :cluster)
        if nrow(group) < 2
          group[:, :cluster] = "na_ms"
        else
          group[:, :order] = range(1, nrow(group))
        end
      end
    end
  end
  return blast_table
end

"""
Summarize information for each cluster
"""
function cluster_numbers(blast_table, all_genes)
  groups = groupby(blast_table, [:species, :chromosome, :cluster])
  n_groups = length(groups)
  # Initialize arrays
  sp_arr = Array(String, n_groups)
  chrom_arr = Array(String, n_groups)
  clust_arr = Array(String, n_groups)
  start_arr = Array(Int, n_groups)
  end_arr = Array(Int, n_groups)
  dups_arr = Array(Int,n_groups)
  total_arr = Array(Int, n_groups)
  prop_arr = Array(Float64, n_groups)
  # annotate each
  for (ix,group)=enumerate(groups)
    n_clustered = nrow(group)
    sp = group[1, :species]
    chrom = group[1, :chromosome]
    cluster = group[1, :cluster]
    start = minimum(group[:start])
    finish = maximum(group[:end])
    all_cluster_tab = all_genes[(all_genes[:species] .== sp)&
    (all_genes[:chromosome] .== chrom)&
    (all_genes[:start] .>= start)&
    (all_genes[:_end] .<= finish), :]
    # not_clustered = length(setdiff(Set(all_cluster_tab[:acc]),
    #                                Set(group[:prot_acc])))
    total_genes = nrow(all_cluster_tab)
    prop_clustered = n_clustered / total_genes

    # Assign to the corresponding arrays
    sp_arr[ix] = sp
    chrom_arr[ix] = chrom
    clust_arr[ix] = cluster
    start_arr[ix] = start
    end_arr[ix] = finish
    dups_arr[ix] = n_clustered
    total_arr[ix] = total_genes
    prop_arr[ix] = prop_clustered
  end
  numbers = DataFrame(species=sp_arr,
  chromosome=chrom_arr,
  cluster=clust_arr,
  start=start_arr,
  _end=end_arr,
  duplicates=dups_arr,
  total_genes=total_arr,
  proportion_duplicates=prop_arr)
  numbers[:, :perc95_gw] = 0.0
  return numbers[numbers[:duplicates] .> 1, :]
end

"""
Given a list of accessions, return the maximum number of duplicates with a
specified E-value that are in that list (which correspond to all the genes
in a region
`acc_list`: all the genes in a region
`duplicates`: Dictionary of parsed blast in the chromosome
`evalue`: Target E-value
"""
f(x) = 1
@everywhere function grab_duplicates(acc_list, duplicates, evalue)
  duplicates_list = []
  duplicates_eval = Dict()
  # Use the accessions that have blast results
  acc_common = intersect(Set(acc_list),Set(keys(duplicates)))
  hit_counts = Dict()
  # Go through the genes in the region
  for acc in acc_common
    # Start hits counter for each accession
    duplicates_eval = 0
    # Each gene can have blast hits on several other
    # genes, go through them
    for hit in duplicates[acc]
      # If the evalue is below or equal to the threshold AND
      # the gene has a blast hit to another gene in the same
      # region, count it.
      if hit[2] <= evalue && hit[1] in acc_list
	duplicates_eval += 1
      end
    end
    # Include the gene only if t
    if duplicates_eval > 1
      hit_counts[acc] = duplicates_eval
    end
  end
  if length(hit_counts) > 0
    return maximum(values(hit_counts))
  else
    return 0
  end
end

"""
Calculate the max-duplicates from one random sample, given a species table
`sp`: species name
`all_genes`: gene annotation
`chrom_table`: table containing chromosome lengths
`region_len`: length of the region being sampled
`evalue`: evalue threshold
"""
function sample_sp(numbers_row, all_genes, chrom_table, args)
  sp = numbers_row[:species]
  region_len = numbers_row[:_end] - numbers_row[:start]
  sp_table = all_genes[all_genes[:species] .== sp, :]
  chroms = chrom_table[(chrom_table[:species] .== sp)&
		       # Only include chromosomes that are
		       # longer to the region being sampled
		       (chrom_table[:length] .> region_len),
		       [:chromosome, :length]]
  cluster = numbers_row[:cluster]
  max_values = @sync @parallel vcat for i=1:args["blast_samples"]

    # Get random chromosome
    sample_up = 0
    if length(chroms[:chromosome]) == 0
      println(format("sp table: {}|region length: {}|species: {}", nrow(sp_table), region_len, sp))
    end
    sample_chrom = sample(chroms[:chromosome])

    # Calculate the up limit for sampling chromosome regions
    sample_space = chroms[chroms[:chromosome] .== sample_chrom, :length] - region_len
    # Calculate sample
    sample_up = sample(range(1, sample_space[1]))
    sample_start, sample_end = (sample_up, sample_up + region_len)
    # Get accessions for the genes in the sample region
    sample_accs = sp_table[(sp_table[:chromosome] .== sample_chrom)&
			   (sp_table[:start] .< sample_end)&
			   (sp_table[:_end] .> sample_start),:acc]
    json_file = readstring(open("$(args["db"])/blasts/$(sp)_$(sample_chrom).json"))
    blast_dic = JSON.parse(json_file)
    max_dups = grab_duplicates(sample_accs, blast_dic, args["evalue"])
    outfile = "$(args["name_family"])/intermediates/samples.coords"
    open(outfile, "a") do fileO
      log = "$(sp),$(cluster),$(sample_chrom),$(sample_start),$(sample_end),$(max_dups)\n"
      write(fileO, log)
    end
    #println(max_dups)
    max_dups
  end
  return percentile(max_values, 95)
end

"""
Calculate the maximum number of duplicates in the samples
specified by `blast_samples` and return the table with the
a column `perc95_gw`
"""
function sample_table(numbers, all_genes, genomes, args)
  col_lengths = ""
  for (ix,col) = enumerate([:species, :cluster])
    spaces = [length(x) for x in numbers[:,col]]
    if length(spaces) > 0
      max_space = maximum(spaces)
    else
      max_space = 0
    end
    col_lengths *= "{:$(max_space + 2)s} "
  end
  #col_lengths *= "{:>12s} {:15s}"
  str_fmt = FormatExpr(col_lengths * "{:>12s}  {:15s}")
  numbers = numbers[numbers[:cluster] .!= "na_ms", :]
  println("$(nrow(numbers)) cluster candidates")
  printfmtln(str_fmt, "Species", "Cluster", "Duplicates", "Percentile 95")
  str_fmt = FormatExpr(col_lengths * "{:>12d}  {:>13.3f}")
  for row = eachrow(numbers)
    if row[:cluster] == "na_ms"
      p95 = 0
      color = :gray
    else
      p95 = sample_sp(row, all_genes, genomes, args)
      if row[:duplicates] >= p95
	color = :green
      else
	color = :red
      end
    end
    row[:perc95_gw] = p95
    msg = format(str_fmt, row[:species], row[:cluster],
		 row[:duplicates], row[:perc95_gw])
    print_with_color(color, msg * "\n")
  end
  tab_file_name = format("{1}/report/{1}_numbers.csv", args["name_family"])
  writetable(tab_file_name, numbers)
  clean_number = numbers[numbers[:duplicates] .>= numbers[:perc95_gw], :]
  clusters = unique(clean_number[:cluster])
  deleteat!(clusters, findin(clusters ,["na_ms","0","na_95"]))
  clean_numbers = clean_number[findin(clean_number[:cluster], clusters), :]
  for_removal = numbers[numbers[:duplicates] .< numbers[:perc95_gw], :]
  tab_clean_file = replace(tab_file_name, ".csv", "_clean.csv")
  writetable(tab_clean_file, clean_numbers)
  return clean_numbers, for_removal
end

"""
Run analysis
"""
function run_ctdg(args)
  if isdir(format("{}/{}", args["out_dir"], args["name_family"]))
    println(format("Results for {} were found in {}",
		  args["out_dir"], args["name_family"]))
  else
    # Check that reference sequence exists
    @assert isfile(args["ref_seq"]) "Reference sequence " * args["ref_seq"] * "does
    not exist"
    # Remove incomplete anlayses
    remove_if_incomplete(args)

    # If species were selected, check if they exist and filter tables
    check_sp(args)

    # Create directory structure
    create_folders(args["name_family"])

    # Run blast and get new query file if run with --iterative
    if args["iterative"]
      pre_blast(args)
    end

    # Run blast
    blast_out_file = format("{1}/intermediates/{1}.blast",args["name_family"])
    blast_exe(blast_path,
    cpus=args["cpu"],
    ref=args["ref_seq"],
    subject=args["db"] * "/all_seqs.fa",
    out_file= blast_out_file,
    evalue=args["evalue"])
    dir = true
    # Parse blast results
    parsed_blast = blast_parse(blast_out_file, sp_list=args["sp"])
    # Run meanshift step
    MeanShift(parsed_blast, all_genes, args["name_family"])
    # Get information about the clusters
    numbers = cluster_numbers(parsed_blast, all_genes)
    # Run sampling step
    clean_numbers, for_removal = sample_table(numbers, all_genes, genomes, args)
    parsed_blast[:order] = [string(x) for x in parsed_blast[:order]]
    for (sp,clu) = zip(for_removal[:species],for_removal[:cluster])
      parsed_blast[(parsed_blast[:species] .== sp)&
		   (parsed_blast[:cluster] .== clu), :cluster] = "na_95"
    end
    # Reannotate the order for excluded clusters
    # This was done here because I did'nt want to change the type of
    # the column but at the very end
    parsed_blast[parsed_blast[:cluster] .== "na_95", :order] = "na_95"
    parsed_blast[parsed_blast[:cluster] .== "na_ms", :order] = "na_ms"
    gene_file = format("{1}/report/{1}_genes.csv",args["name_family"])
    writetable(gene_file, parsed_blast)
    # Filter out non-clustered genes
    gene_clean_file = replace(gene_file, "genes","genes_clean")
    # Remove non-clustered order values
    orders = unique(parsed_blast[:cluster])
    deleteat!(orders, findin(orders ,["na_ms","0","na_95"]))
    # Filter out non-clustered genes
    genes_clean = parsed_blast[findin(parsed_blast[:cluster], orders), :]
    writetable(gene_clean_file, genes_clean)
    # Compress and save intermediate files
    clean_intermediates(args)
    # Move analysis files to output directory
    move_finished(args)
  end
end

"""
Clean intermediates
"""
function clean_intermediates(args)
  intermediates = readdir("$(args["name_family"])/intermediates/")
  w = ZipFile.Writer("$(args["name_family"])/intermediates/intermediates.zip")
  for file=intermediates
    f = ZipFile.addfile(w, file)
    file = "$(args["name_family"])/intermediates/"* file
    write(f, read(open(file)))
    Base.rm(file)
  end
  close(w)
end

"""
Move finished analysis to output directory
"""
function move_finished(args)
  if ! ispath(args["out_dir"])
    mkdir(args["out_dir"])
  end
  mv(args["name_family"], "$(args["out_dir"])/$(args["name_family"])")
end

"""
Check if a batch analysis needs to be run and
execute analysis
"""
function check_d_run(args)
  if isdir(args["dir"])
    println("Running batch analysis")
    counter = 0
    ref_path = readdir(args["dir"])
    num_refs = length(ref_path)
    for fasta = ref_path
      println(fasta)
      counter += 1
      args["ref_seq"] = "$(args["dir"])/$(fasta)"
      if isfile(args["ref_seq"])
        args["name_family"] = split(fasta, '.')[1]
        println(format("Running analysis {} ({} of {})",
                 args["name_family"],
                 counter, num_refs))
        run_ctdg(args)
        clustered_genes = readtable(format("{1}/{2}/report/{2}_genes_clean.csv",
                  args["out_dir"], args["name_family"]))
        if ! isdir("skipped")
          mkdir("skipped")
        end
        move_accs = clustered_genes[:prot_acc]
        accs_to_move = length(move_accs)
        accs_passed = 0
        #println(move_accs[1])
        for seq=readdir(args["dir"])

          for acc=move_accs
            if contains(seq, acc)
              seq = format("{}/{}",args["dir"], seq)
              dest = "skipped/" * (split(seq, "/")[end])
              if isfile(seq)
                mv(seq, dest)
              end
              accs_passed += 1
            end
          end
          if accs_passed == accs_to_move
            break
          end

        end
        println("Source: " * args["ref_seq"])
        println(format("Destination: {1}/{2}/{2}.fa", args["out_dir"], args["name_family"]))
        seq1 = args["ref_seq"]
        dest1 = format("{1}/{2}/{2}.fa", args["out_dir"], args["name_family"])
        if isfile(seq1)
          mv(seq1, dest1)
        end
      end
    end
  else

    run_ctdg(args)
  end
end
## Functions end

# Check if Blast exists
blast_path, blast_exists = test_blast()
@assert blast_exists "BlastP does not exist in PATH"

# Parse options
args = parse_commandline()
all_genes, genomes = check_db(args["db"])
@time check_d_run(args)
