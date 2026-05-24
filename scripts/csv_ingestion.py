import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from core.database import supabase

CSV_PATH = os.path.join(os.path.dirname(__file__), "raw_orion_export.csv")

COLUMN_MAP = {
    "Entity Legal Name": "entity_name",
    "Tax ID Number": "tax_id",
    "Client Email": "primary_email",
    "Address": "mailing_address",
    "Entity Type": "entity_type",
}


def ingest_csv():
    df = pd.read_csv(CSV_PATH)

    df = df.rename(columns=COLUMN_MAP)

    df = df.dropna(subset=["entity_name"])
    df = df.where(pd.notnull(df), None)

    records = df.to_dict(orient="records")

    processed = 0
    for record in records:
        supabase.table("investors").upsert(record, on_conflict="entity_name").execute()
        processed += 1

    print(f"Ingestion complete: {processed} entity record(s) processed into the investors table.")


if __name__ == "__main__":
    try:
        ingest_csv()
    except Exception as e:
        print(f"Error during CSV ingestion: {e}")
