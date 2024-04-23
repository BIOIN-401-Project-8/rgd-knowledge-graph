import argparse
import asyncio
import glob
import gzip
import itertools
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import lxml.etree as ET
import neo4j
import numpy as np
import pandas as pd
from tqdm import tqdm
from tqdm.contrib.concurrent import process_map


def get_relation_df(file: str):
    df = pd.read_csv(file, sep="\t")
    try:
        df[["1st Type", "1st Concept ID"]] = df["1st"].str.split("|", n=1, expand=True)
        df[["2nd Type", "2nd Concept ID"]] = df["2nd"].str.split("|", n=1, expand=True)
    except ValueError:
        logging.exception(f"Error parsing {file}")
        return pd.DataFrame()
    df.drop(columns=["1st", "2nd"], inplace=True)
    return df


async def run_relation_queries(session: neo4j.AsyncSession, input_dirs: list[str]):
    files = []
    for input_dir in input_dirs:
        for file in glob.glob(f"{input_dir}/*.tsv"):
            files.append(file)
    # pmids = [int(Path(file).stem) for file in files]
    # pmid_date_lookup = get_pmid_date_lookup(pmids)

    logging.info(f"Processing {len(files)} files")
    if Path("/data/rgd-knowledge-graph/aggrelation2pubtator3.tsv").exists():
        df = pd.read_csv("/data/rgd-knowledge-graph/aggrelation2pubtator3.tsv", sep="\t")
    else:
        df = pd.DataFrame()
    if files:
        agg_dfs = [df]
        batch_size = 128000
        for files_batch in tqdm(batch(files, n=batch_size), total=len(files) // batch_size + 1):
            dfs = process_map(get_relation_df, files_batch, chunksize=1000, max_workers=24, disable=True)
            concat_df = pd.concat(dfs)
            del dfs
            agg_df = agg_relations(concat_df)
            agg_dfs.append(agg_df)
        df = pd.concat(agg_dfs)
        del agg_dfs
        df = agg_relations(df)
        # df["PubDate"] = df["PMID"].map(pmid_date_lookup)
        # df["PubDate"] = df["PubDate"].fillna(np.nan).replace([np.nan], [None])
        df.to_csv("/data/rgd-knowledge-graph/aggrelation2pubtator3.tsv", sep="\t", index=False)

    grouped_df = df.groupby(["1st Type", "2nd Type", "Type"])
    for [node_1st_type, node_2nd_type, relation_type], df_type in tqdm(sorted(grouped_df, key=lambda k: len(k[1]))):
        logging.info(f"Creating {len(df_type)} {relation_type} relations")
        query = f"""
            CALL apoc.periodic.iterate(
                "UNWIND $rows as row RETURN row",
                "MATCH (a:`{node_1st_type}`:PubTator3 {{ConceptID: row['1st Concept ID']}})
                MATCH (b:`{node_2nd_type}`:PubTator3 {{ConceptID: row['2nd Concept ID']}})
                MERGE (a)-[r:`{relation_type}_PubTator3` {{PMID: row['PMID']}}]->(b)",
                {{batchSize: 10000, batchMode: "BATCH", parallel: false, params: {{rows: $rows}}}}
            )
        """
        # query = f"""
        #     CALL apoc.periodic.iterate(
        #         "UNWIND $rows as row RETURN row",
        #         "MATCH (a:`{node_1st_type}`:PubTator3 {{ConceptID: row['1st Concept ID']}})
        #         MATCH (b:`{node_2nd_type}`:PubTator3 {{ConceptID: row['2nd Concept ID']}})
        #         MERGE (a)-[r:`{relation_type}_PubTator3` {{PMID: row['PMID']}}]->(b)
        #         SET r.PubDate = row['PubDate']",
        #         {{batchSize: 10000, batchMode: "BATCH", parallel: false, params: {{rows: $rows}}}}
        #     )
        # """
        await run_query(session, query, rows=df_type.to_dict("records"))

def batch(iterable, n=1):
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx : min(ndx + n, l)]


def item_to_list(x):
    if isinstance(x, str):
        return x.split("|")
    elif isinstance(x, list):
        return x
    else:
        return [x]


def unique_list(xs):
    return sorted(
        set(
            itertools.chain.from_iterable(

                map(item_to_list, xs.dropna())
            )
        )
    )


def agg_bioconcepts(df: pd.DataFrame):
    df = df.groupby("Concept ID").agg(
        {
            "PMID": unique_list,
            "Type": "first",
            "Mentions": unique_list,
            "Resource": unique_list,
        }
    )
    df = df.reset_index()
    return df


def agg_relations(df: pd.DataFrame):
    df = df.groupby(["1st Type", "1st Concept ID", "2nd Type", "2nd Concept ID", "Type"]).agg(
        {
            "PMID": unique_list,
            # "PubDate": sorted_unique_list,
        }
    )
    df = df.reset_index()
    return df

def get_pmid_date(pmid: int):
    date = None
    try:
        padded_pmid = f"{pmid:08d}"
        in_pubmed_path = (
            Path("/data/Archive/pubmed/Archive") / padded_pmid[0:2] / padded_pmid[2:4] / padded_pmid[4:6] / f"{pmid}.xml.gz"
        )
        with gzip.open(in_pubmed_path, "rb") as f:
            tree = ET.parse(f)
            root = tree.getroot()
            published_date = root.find(".//PubDate")
            from datetime import datetime
            if published_date is not None:
                try:
                    year = published_date.find('Year')
                    year = year.text if year is not None else None
                    month = published_date.find('Month')
                    month = month.text if month is not None else None
                    day = published_date.find('Day')
                    day = day.text if day is not None else None
                    if year and month and day:
                        date = datetime.strptime(
                            f"{year}-{month}-{day}",
                            "%Y-%b-%d",
                        )
                    elif year and month:
                        date = datetime.strptime(
                            f"{year}-{month}",
                            "%Y-%b",
                        )
                    elif year:
                        date = datetime.strptime(
                            f"{year}",
                            "%Y",
                        )
                except AttributeError:
                    logging.exception(f"Error parsing date for {pmid}")
    except (FileNotFoundError, ET.XMLSyntaxError):
        logging.exception(f"Error parsing date for {pmid}")
    return pmid, datetime.strftime(date, "%Y-%m-%d") if date else None


def get_pmid_date_lookup(pmids):
    logging.info(f"Getting PMID date lookup for {len(pmids)} PMIDs")
    return {}
    # if Path("/data/rgd-knowledge-graph/pmid_date_lookup.json").exists():
    #     with open("/data/rgd-knowledge-graph/pmid_date_lookup.json", "r") as f:
    #         pmid_date_lookup = json.load(f)
    #         pmid_date_lookup = {int(k): v for k, v in pmid_date_lookup.items()}
    #         return pmid_date_lookup
    pmid_date_lookup = dict(process_map(get_pmid_date, pmids, chunksize=1000, max_workers=24))
    with open("/data/rgd-knowledge-graph/pmid_date_lookup.json", "w") as f:
        json.dump(pmid_date_lookup, f)
    return pmid_date_lookup


def load_bioconcepts_queries_df(file: str):
    if Path(file).stat().st_size == 0:
        return pd.DataFrame()
    df_file = pd.read_csv(file, sep="\t", dtype={"PMID": str})
    df_file = df_file[df_file["Concept ID"] != "-"]
    return df_file


async def run_bioconcepts_queries(session: neo4j.AsyncSession, input_dirs: list[str]):
    files = []
    for input_dir in input_dirs:
        files.extend(glob.glob(f"{input_dir}/*.tsv"))

    if Path("/data/rgd-knowledge-graph/aggbioconcepts2pubtator3.tsv").exists():
        df = pd.read_csv("/data/rgd-knowledge-graph/aggbioconcepts2pubtator3.tsv", sep="\t")
    else:
        df = pd.DataFrame()

    files = sorted(files)
    logging.info(f"Processing {len(files)} files")
    if files:
        for files_batch in tqdm(batch(files, n=64000), total=len(files) // 64000 + 1):
            dfs = process_map(load_bioconcepts_queries_df, files_batch, chunksize=1000, max_workers=24, disable=True)
            df = pd.concat([df] + dfs)
            del dfs
            df = agg_bioconcepts(df)
        df.to_csv("/data/rgd-knowledge-graph/aggbioconcepts2pubtator3.tsv", sep="\t", index=False)

    for node_type, df_type in tqdm(df.groupby("Type")):
        logging.info(f"Creating constraint on {node_type} nodes")
        query = f"CREATE CONSTRAINT IF NOT EXISTS FOR (a:`{node_type}`) REQUIRE a.ConceptID IS UNIQUE"
        await run_query(session, query)
        logging.info(f"Creating {len(df_type)} {node_type} nodes")
        query = f"""
            CALL apoc.periodic.iterate(
                "UNWIND $rows as row RETURN row",
                "MERGE (a:`PubTator3`:`{node_type}` {{ConceptID: row['Concept ID'], Mentions: row['Mentions'], PMID: row['PMID'], Resource: row['Resource']}})",
                {{batchSize: 10000, batchMode: "BATCH", concurrency: 8, parallel: true, params: {{rows: $rows}}}}
            )
        """
        await run_query(session, query, rows=df_type.to_dict("records"))


async def run_query(session: neo4j.AsyncSession, query: str, **kwargs):
    logging.debug(query)
    await session.run(query, **kwargs)


async def main():
    parser = argparse.ArgumentParser(description="ingest")
    parser.add_argument("--neo4j_uri", help="neo4j uri", default="bolt://neo4j:7687")
    parser.add_argument("--neo4j_user", help="neo4j user", default="neo4j")
    parser.add_argument("--neo4j_password", help="neo4j password", default=os.environ.get("NEO4J_PASSWORD"))
    parser.add_argument("--neo4j_database", help="neo4j database", default="neo4j")
    parser.add_argument(
        "--input_relation_dirs",
        nargs="+",
        help="input directory",
        default=[
            # "/data/rgd-knowledge-graph/pubtator3/ftp/relation2pubtator3",
            # "/data/rgd-knowledge-graph/pubtator3/api/relation2pubtator3",
            # "/data/rgd-knowledge-graph/pubtator3/local/relation2pubtator3",
        ],
    )
    parser.add_argument(
        "--input_bioconcepts_dirs",
        nargs="+",
        help="input directory",
        default=[
            # "/data/rgd-knowledge-graph/pubtator3/ftp/bioconcepts2pubtator3",
            # "/data/rgd-knowledge-graph/pubtator3/api/bioconcepts2pubtator3",
            # "/data/rgd-knowledge-graph/pubtator3/local/bioconcepts2pubtator3",
        ],
    )
    args = parser.parse_args()

    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    logs_path = Path("logs")
    logs_path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG, format=log_format, filename=logs_path / datetime.now().strftime("%Y-%m-%d_%H-%M-%S.log")
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(console)

    async with neo4j.AsyncGraphDatabase.driver(
        uri=args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password), database=args.neo4j_database
    ) as driver:
        async with driver.session(database=args.neo4j_database) as session:
            # await run_bioconcepts_queries(session, args.input_bioconcepts_dirs)
            await run_relation_queries(session, args.input_relation_dirs)


if __name__ == "__main__":
    asyncio.run(main())
