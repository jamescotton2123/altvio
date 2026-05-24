import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from core.database import supabase


def seed_triad():
    # Step 1: Fetch an existing investor
    investor_response = supabase.table("investors").select("id").limit(1).execute()

    if not investor_response.data:
        print("Error: No investors found. Run seed_data.py first to populate the investors table.")
        return

    investor_id = investor_response.data[0]["id"]
    print(f"Step 1 Success: Found investor -> {investor_id}")

    # Step 2: Insert a new deal
    deal_response = (
        supabase.table("deals")
        .insert({"offering_name": "Project Alpha Fund I", "target_raise": 50000000})
        .execute()
    )

    deal_id = deal_response.data[0]["id"]
    print(f"Step 2 Success: Deal created -> {deal_id} ('Project Alpha Fund I', $50,000,000 target)")

    # Step 3: Insert a commitment linking investor and deal
    commitment_response = (
        supabase.table("commitments")
        .insert(
            {
                "investor_id": investor_id,
                "deal_id": deal_id,
                "committed_amount": 250000,
            }
        )
        .execute()
    )

    commitment_id = commitment_response.data[0]["id"]
    print(f"Step 3 Success: Commitment created -> {commitment_id} ($250,000 committed)")
    print("\nTriad seed complete. Investor -> Deal -> Commitment chain is live in the database.")


if __name__ == "__main__":
    try:
        seed_triad()
    except Exception as e:
        print(f"Error during triad seed: {e}")
