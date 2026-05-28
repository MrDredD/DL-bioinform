"""Python port of ``chromosome_means_function.R``.

The R script defines two helpers, ``get_chromosome_means`` (legacy, TSV mapping)
and ``get_chromosome_means_v2`` (current, CSV mapping).  Both build a
cells-by-chromosomes matrix of average log-normalised expression, the kind of
feature used as an inferCNV-style proxy for chromosome-level expression in
glioblastoma scRNA-seq.

The Python versions keep the same pipeline:
    1. drop genes with zero total count
    2. library-size normalisation to ``scale_factor`` (1000 in the R original)
    3. ``log2(x + 1)`` transform
    4. drop low-expressed genes (sum of log-values across cells < threshold)
    5. drop HLA genes (hyper-variable locus that confounds CNV signals)
    6. for every chromosome in the mapping table, average log-expression of
       intersecting genes per cell.

Input is an :class:`anndata.AnnData` with **raw counts in ``adata.X``** and
gene symbols in ``adata.var_names``.  Output is a ``pandas.DataFrame`` indexed
by cell barcodes with one column per chromosome.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import scipy.sparse as sp
from anndata import AnnData


def _as_csr(matrix) -> sp.csr_matrix:
    if sp.issparse(matrix):
        return matrix.tocsr()
    return sp.csr_matrix(matrix)


def get_chromosome_means_v2(
    adata: AnnData,
    path_to_mapped_genes: str = "genes_chr_mapping.csv",
    *,
    min_log_sum: float = 100.0,
    scale_factor: float = 1000.0,
    drop_hla: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """Port of ``get_chromosome_means_v2`` from ``chromosome_means_function.R``.

    Parameters
    ----------
    adata
        Raw counts in ``adata.X`` (cells x genes), gene symbols in
        ``adata.var_names``.
    path_to_mapped_genes
        CSV with columns ``Gene_name`` and ``Chromosome``.
    min_log_sum
        Per-gene threshold on ``sum_i log2(norm_i + 1)`` across cells.
    scale_factor
        Library-size normalisation target (R original uses ``1000``; the
        canonical scanpy choice is ``1e4``).
    drop_hla
        Whether to drop genes whose symbol contains ``"HLA"``.
    verbose
        Mirror the R ``cat(...)`` per-chromosome progress messages.

    Returns
    -------
    pandas.DataFrame
        Cells x chromosomes mean log-normalised expression.
    """
    counts = _as_csr(adata.X)
    var_names = np.asarray(adata.var_names)
    obs_names = np.asarray(adata.obs_names)

    # 1) drop globally-zero genes
    gene_totals = np.asarray(counts.sum(axis=0)).ravel()
    keep_gene = gene_totals > 0
    counts = counts[:, keep_gene]
    var_names = var_names[keep_gene]

    # 2) library-size normalisation per cell -> scale_factor
    lib = np.asarray(counts.sum(axis=1)).ravel()
    lib[lib == 0] = 1.0  # avoid division by zero for empty droplets
    cell_scale = sp.diags(scale_factor / lib)
    norm = cell_scale @ counts  # cells x genes

    # 3) log2(x + 1); sparse-friendly because log2(0 + 1) = 0
    log_counts = norm.copy()
    log_counts.data = np.log2(log_counts.data + 1.0)

    # 4) keep only genes whose log-expression sums above min_log_sum
    gene_log_sum = np.asarray(log_counts.sum(axis=0)).ravel()
    keep_gene = gene_log_sum >= min_log_sum
    log_counts = log_counts[:, keep_gene]
    var_names = var_names[keep_gene]

    # 5) drop HLA genes (matches R `grep("HLA", rownames(...))`)
    if drop_hla:
        is_hla = np.array(["HLA" in g for g in var_names])
        log_counts = log_counts[:, ~is_hla]
        var_names = var_names[~is_hla]

    # 6) per-chromosome mean across the intersecting genes
    gene_map = pd.read_csv(path_to_mapped_genes)
    if not {"Gene_name", "Chromosome"}.issubset(gene_map.columns):
        raise ValueError(
            "Gene mapping must have 'Gene_name' and 'Chromosome' columns; got "
            f"{list(gene_map.columns)}"
        )

    gene_to_idx = {g: i for i, g in enumerate(var_names)}

    n_cells = log_counts.shape[0]
    chrom_means: dict[str, np.ndarray] = {}
    for chrom in gene_map["Chromosome"].unique():
        chrom_genes = gene_map.loc[gene_map["Chromosome"] == chrom, "Gene_name"]
        idx = [gene_to_idx[g] for g in chrom_genes if g in gene_to_idx]
        if verbose:
            print(f"Chromosome {chrom}: mapped {len(idx)} genes")
        if idx:
            sub = log_counts[:, idx]
            chrom_means[f"Chr{chrom}"] = np.asarray(sub.mean(axis=1)).ravel()
        else:
            chrom_means[f"Chr{chrom}"] = np.full(n_cells, np.nan)

    return pd.DataFrame(chrom_means, index=obs_names)


def get_chromosome_means(
    adata: AnnData,
    path_to_mapped_genes: str,
    *,
    min_log_sum: float = 100.0,
    scale_factor: float = 1000.0,
    drop_hla: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """Legacy variant matching the first R function.

    Identical pipeline, but the mapping file is a TSV with columns
    ``HGNC.symbol`` and ``Chromosome.scaffold.name`` (the Ensembl BioMart
    export the original R code consumed).
    """
    counts = _as_csr(adata.X)
    var_names = np.asarray(adata.var_names)
    obs_names = np.asarray(adata.obs_names)

    gene_totals = np.asarray(counts.sum(axis=0)).ravel()
    keep_gene = gene_totals > 0
    counts = counts[:, keep_gene]
    var_names = var_names[keep_gene]

    lib = np.asarray(counts.sum(axis=1)).ravel()
    lib[lib == 0] = 1.0
    norm = sp.diags(scale_factor / lib) @ counts

    log_counts = norm.copy()
    log_counts.data = np.log2(log_counts.data + 1.0)

    gene_log_sum = np.asarray(log_counts.sum(axis=0)).ravel()
    keep_gene = gene_log_sum >= min_log_sum
    log_counts = log_counts[:, keep_gene]
    var_names = var_names[keep_gene]

    if drop_hla:
        is_hla = np.array(["HLA" in g for g in var_names])
        log_counts = log_counts[:, ~is_hla]
        var_names = var_names[~is_hla]

    gene_map = pd.read_csv(path_to_mapped_genes, sep="\t")
    sym_col = "HGNC.symbol"
    chr_col = "Chromosome.scaffold.name"
    if not {sym_col, chr_col}.issubset(gene_map.columns):
        raise ValueError(
            f"Expected columns '{sym_col}' and '{chr_col}' in {path_to_mapped_genes}"
        )

    gene_to_idx = {g: i for i, g in enumerate(var_names)}
    # R original labels columns Chr1..Chr22 in order, regardless of mapping content
    autosomes = [str(i) for i in range(1, 23)]
    n_cells = log_counts.shape[0]
    result: dict[str, np.ndarray] = {}
    for chrom in autosomes:
        chrom_genes = gene_map.loc[gene_map[chr_col].astype(str) == chrom, sym_col]
        idx = [gene_to_idx[g] for g in chrom_genes if g in gene_to_idx]
        if verbose:
            print(f"Chromosome {chrom}: mapped {len(idx)} genes")
        if idx:
            sub = log_counts[:, idx]
            result[f"Chr{chrom}"] = np.asarray(sub.mean(axis=1)).ravel()
        else:
            result[f"Chr{chrom}"] = np.full(n_cells, np.nan)
    return pd.DataFrame(result, index=obs_names)


__all__ = ["get_chromosome_means", "get_chromosome_means_v2"]
