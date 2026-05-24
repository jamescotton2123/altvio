import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from core.database import supabase


def upload_dummy_investors():
    dummy_investors = [
        {
            "entity_name": "Blackstone Growth Partners",
            "total_commitment": 5000000,
            "unfunded_commitment": 1250000,
        },
        {
            "entity_name": "Sequoia Capital Ventures",
            "total_commitment": 3200000,
            "unfunded_commitment": 800000,
        },
        {
            "entity_name": "Andreessen Horowitz Fund III",
            "total_commitment": 7500000,
            "unfunded_commitment": 2100000,
        },
    ]

    df = pd.DataFrame(dummy_investors)

    try:
        response = supabase.table("investors").insert(df.to_dict(orient="records")).execute()
        print(f"Success: {len(response.data)} investor(s) added to the database.")
    except Exception as e:
        print(f"Error uploading investors: {e}")


if __name__ == "__main__":
    upload_dummy_investors()
