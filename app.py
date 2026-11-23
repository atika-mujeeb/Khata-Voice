import os
import json
import time
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Optional, Literal

import pandas as pd
import streamlit as st
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from google.genai.errors import APIError

# ==============================================================================
# CONFIGURATION & CONSTANTS
# ==============================================================================
MODEL_NAME = "gemini-3.5-flash-lite"
DB_PATH = "khata_voice.db"
HIGH_BALANCE_THRESHOLD = 3000  # PKR threshold for red alert styling

st.set_page_config(
    page_title="Khata Voice — Digital Udhaar Register",
    page_icon="📒",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for mobile-friendly touch targets and crisp metrics
st.markdown(
    """
    <style>
    .stButton > button {
        width: 100%;
        border-radius: 10px;
        height: 3em;
        font-weight: 600;
    }
    div[data-testid="stMetric"] {
        background-color: #f8f9fa;
        padding: 15px;
        border-radius: 10px;
        border: 1px solid #e9ecef;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==============================================================================
# PYDANTIC SCHEMAS FOR GEMINI STRUCTURED OUTPUT
# ==============================================================================
class ItemDetail(BaseModel):
    name: str = Field(description="Item name in Roman Urdu, e.g., 'chawal', 'anday'")
    quantity: Optional[float] = Field(default=None, description="Numeric quantity, e.g., 2, 1.5")
    unit: Optional[str] = Field(default=None, description="Unit like 'kg', 'dozen', 'packet', 'liter'")


class ExtractedTransaction(BaseModel):
    customer_name: str = Field(
        description="Normalized customer name matching existing customers if close (e.g., Ahmad -> Ahmed)"
    )
    type: Literal["udhaar", "payment"] = Field(
        description="'udhaar' if credit given/items bought on credit, 'payment' if cash/money returned/received"
    )
    items: List[ItemDetail] = Field(
        default_factory=list, description="List of grocery items if mentioned"
    )
    amount_pkr: Optional[int] = Field(
        default=None, description="Total amount in Pakistani Rupees (PKR), or null if not spoken/written"
    )
    transcript: str = Field(description="Clean Roman Urdu transcription of what was understood for this entry")
    confidence: float = Field(description="Confidence score between 0.0 and 1.0")


class KhataResponse(BaseModel):
    intent: Literal["record_transaction", "balance_query", "unknown"] = Field(
        description="Whether user is logging transactions or asking for a customer's remaining balance (baqaya)"
    )
    query_customer_name: Optional[str] = Field(
        default=None, description="If intent is balance_query, the normalized customer name being asked about"
    )
    transactions: List[ExtractedTransaction] = Field(
        default_factory=list, description="Extracted transactions when intent is record_transaction"
    )


# ==============================================================================
# DATABASE LAYER (SQLITE)
# ==============================================================================
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                phone TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('udhaar', 'payment')),
                items_json TEXT,
                amount INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            )
            """
        )
        # Seed demo data on very first launch if empty
        count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        if count == 0:
            seed_demo_data(conn)


def seed_demo_data(conn: Optional[sqlite3.Connection] = None):
    close_after = False
    if conn is None:
        conn = get_conn()
        close_after = True

    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM customers")

    demo_customers = [
        ("Ahmed Khan", "923001234567"),
        ("Bilal Raza", "923219876543"),
        ("Sana Baji", "923334567890"),
        ("Tariq Mehmood", "923015556677"),
        ("Usman Ghani", "923451112233"),
        ("Zainab Bibi", "923128889900"),
        ("Kamran Akmal", "923007778899"),
        ("Farhan Saeed", ""),
        ("Haji Sahab", "923223334455"),
    ]

    cust_ids = {}
    for name, phone in demo_customers:
        cur = conn.execute("INSERT INTO customers (name, phone) VALUES (?, ?)", (name, phone))
        cust_ids[name] = cur.lastrowid

    now = datetime.now()
    yesterday = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)

    demo_txns = [
        # Ahmed Khan (Balance: 4200)
        (cust_ids["Ahmed Khan"], "udhaar", [{"name": "Aata", "quantity": 10, "unit": "kg"}, {"name": "Cheeni", "quantity": 3, "unit": "kg"}], 2700, two_days_ago),
        (cust_ids["Ahmed Khan"], "udhaar", [{"name": "Cooking Oil", "quantity": 2, "unit": "liter"}], 1500, now),
        # Bilal Raza (Balance: 1200)
        (cust_ids["Bilal Raza"], "udhaar", [{"name": "Chawal", "quantity": 5, "unit": "kg"}], 1500, yesterday),
        (cust_ids["Bilal Raza"], "payment", [], 300, now),
        # Sana Baji (Balance: 3650)
        (cust_ids["Sana Baji"], "udhaar", [{"name": "Dal Chana", "quantity": 2, "unit": "kg"}, {"name": "Patti", "quantity": 1, "unit": "pack"}], 3000, two_days_ago),
        (cust_ids["Sana Baji"], "udhaar", [{"name": "Anday", "quantity": 1, "unit": "dozen"}, {"name": "Dahi", "quantity": 2, "unit": "pack"}], 650, now),
        # Tariq Mehmood (Balance: 5400)
        (cust_ids["Tariq Mehmood"], "udhaar", [{"name": "Monthly Rashan", "quantity": 1, "unit": "bundle"}], 6400, yesterday),
        (cust_ids["Tariq Mehmood"], "payment", [], 1000, now),
        # Usman Ghani (Balance: 850)
        (cust_ids["Usman Ghani"], "udhaar", [{"name": "Doodh", "quantity": 2, "unit": "liter"}, {"name": "Double Roti", "quantity": 2, "unit": "pack"}], 850, yesterday),
        # Zainab Bibi (Balance: 2100)
        (cust_ids["Zainab Bibi"], "udhaar", [{"name": "Ghee", "quantity": 3, "unit": "kg"}], 2100, two_days_ago),
        # Kamran Akmal (Balance: 0 - settled)
        (cust_ids["Kamran Akmal"], "udhaar", [{"name": "Cold Drink", "quantity": 2, "unit": "bottle"}], 400, yesterday),
        (cust_ids["Kamran Akmal"], "payment", [], 400, now),
        # Farhan Saeed (Balance: 1750)
        (cust_ids["Farhan Saeed"], "udhaar", [{"name": "Basmati Rice", "quantity": 5, "unit": "kg"}], 1750, yesterday),
        # Haji Sahab (Balance: 3100)
        (cust_ids["Haji Sahab"], "udhaar", [{"name": "Dry Fruits & Tea", "quantity": 1, "unit": "pack"}], 3100, two_days_ago),
    ]

    for cid, ttype, items, amt, ts in demo_txns:
        conn.execute(
            "INSERT INTO transactions (customer_id, type, items_json, amount, created_at) VALUES (?, ?, ?, ?, ?)",
            (cid, ttype, json.dumps(items), amt, ts.strftime("%Y-%m-%d %H:%M:%S")),
        )

    conn.commit()
    if close_after:
        conn.close()


def get_existing_customer_names() -> List[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM customers ORDER BY name").fetchall()
        return [r["name"] for r in rows]


def get_or_create_customer(name: str) -> int:
    clean_name = name.strip().title()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM customers WHERE LOWER(name) = LOWER(?)", (clean_name,)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO customers (name, phone) VALUES (?, ?)", (clean_name, "")
        )
        conn.commit()
        return cur.lastrowid


def save_transaction(customer_name: str, ttype: str, items_str: str, amount: int):
    cid = get_or_create_customer(customer_name)
    items_list = [{"name": items_str, "quantity": None, "unit": None}] if items_str.strip() else []
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO transactions (customer_id, type, items_json, amount, created_at) VALUES (?, ?, ?, ?, ?)",
            (cid, ttype, json.dumps(items_list), int(amount), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()


def get_ledger_df() -> pd.DataFrame:
    query = """
        SELECT 
            c.id,
            c.name AS Customer,
            COALESCE(c.phone, '') AS Phone,
            COALESCE(SUM(CASE WHEN t.type = 'udhaar' THEN t.amount ELSE 0 END), 0) AS Total_Udhaar,
            COALESCE(SUM(CASE WHEN t.type = 'payment' THEN t.amount ELSE 0 END), 0) AS Total_Paid,
            COALESCE(SUM(CASE WHEN t.type = 'udhaar' THEN t.amount ELSE -t.amount END), 0) AS Balance
        FROM customers c
        LEFT JOIN transactions t ON c.id = t.customer_id
        GROUP BY c.id, c.name, c.phone
        ORDER BY Balance DESC, c.name ASC
    """
    with get_conn() as conn:
        return pd.read_sql_query(query, conn)


def get_today_stats() -> tuple[int, int]:
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        udhaar = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='udhaar' AND created_at LIKE ?",
            (f"{today_prefix}%",),
        ).fetchone()[0]
        paid = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='payment' AND created_at LIKE ?",
            (f"{today_prefix}%",),
        ).fetchone()[0]
    return int(udhaar), int(paid)


def get_customer_history(customer_id: int) -> pd.DataFrame:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT created_at, type, items_json, amount FROM transactions WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        ).fetchall()

    records = []
    for r in rows:
        items_formatted = "-"
        if r["items_json"]:
            try:
                parsed = json.loads(r["items_json"])
                parts = []
                for it in parsed:
                    q = f"{it['quantity']:g} " if it.get("quantity") else ""
                    u = f"{it['unit']} " if it.get("unit") else ""
                    parts.append(f"{q}{u}{it['name']}".strip())
                if parts:
                    items_formatted = ", ".join(parts)
            except Exception:
                items_formatted = r["items_json"]

        records.append(
            {
                "Date & Time": r["created_at"],
                "Type": "Udhaar (+)" if r["type"] == "udhaar" else "Wapsi / Paid (-)",
                "Items": items_formatted,
                "Amount (PKR)": r["amount"],
            }
        )
    return pd.DataFrame(records)


# ==============================================================================
# GEMINI AI INTEGRATION
# ==============================================================================
def get_gemini_client() -> Optional[genai.Client]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        try:
            api_key = st.secrets.get("GEMINI_API_KEY")
        except Exception:
            api_key = None

    if not api_key:
        return None
    return genai.Client(api_key=api_key)


def call_gemini_with_retry(fn, max_retries: int = 3):
    """Retries a Gemini API callable on 429/503 with exponential backoff (2s, 4s, 8s)."""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            status_code = getattr(e, "code", None) or getattr(e, "status_code", None)
            err_str = str(e)
            is_retryable = (
                (isinstance(e, APIError) and status_code in (429, 503))
                or status_code in (429, 503)
                or "503" in err_str
                or "429" in err_str
            )
            if is_retryable and attempt < max_retries:
                time.sleep(2 ** (attempt + 1))  # 2s, 4s, 8s
                continue
            raise


def extract_from_gemini(
    audio_bytes: Optional[bytes] = None,
    audio_mime: str = "audio/wav",
    text_input: Optional[str] = None,
) -> KhataResponse:
    client = get_gemini_client()
    if not client:
        raise ValueError("GEMINI_API_KEY missing. Please set it in your environment or .streamlit/secrets.toml.")

    existing_names = get_existing_customer_names()
    names_context = ", ".join(existing_names) if existing_names else "None yet"

    system_prompt = f"""
    You are an intelligent accounting assistant for a Pakistani kiryana (grocery) shopkeeper.
    The shopkeeper speaks or types in Urdu, Roman Urdu, or mixed English-Urdu.

    Existing customers in the register: [{names_context}]

    Rules:
    1. Determine if the user is recording one or more transactions ('record_transaction') OR asking a question about someone's balance/baqaya ('balance_query', e.g., "Ahmed ka kitna baqaya hai?").
    2. Normalize customer names to match the existing customer list if spelled similarly (e.g., "Ahmad" -> "Ahmed Khan" if "Ahmed Khan" is in the list; "Sana" -> "Sana Baji"). If it's a new person, capitalize cleanly.
    3. A single sentence may contain multiple customers or multiple transactions (e.g. "Ahmed ko 500 ka aata aur Bilal ne 200 wapis kiye"). Extract each separately.
    4. 'udhaar' = items given on credit / customer owes money. 'payment' = customer paid back / wapsi / jama karwaye.
    5. If the amount in PKR is not stated, set amount_pkr to null and lower confidence below 0.65.
    6. If audio is noisy, ambiguous, or unclear, lower confidence below 0.65.
    """

    contents = []
    if audio_bytes:
        contents.append(types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime))
        contents.append("Listen to this shopkeeper voice note and extract the khata entry or balance query.")
    if text_input:
        contents.append(f"Shopkeeper input: {text_input}")

    response = call_gemini_with_retry(
        lambda: client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=KhataResponse,
                temperature=0.1,
            ),
        )
    )

    return KhataResponse.model_validate_json(response.text)


def generate_reminder_text(customer_name: str, balance: int, tone: str) -> str:
    client = get_gemini_client()
    if not client:
        # Fallback template if offline/no key
        if tone == "Friendly":
            return f"Assalam-o-Alaikum {customer_name} bhai/baji, umeed hai aap khairiyat se honge. Aap ke khata mein Rs. {balance:,} baqaya hain. Jab sahulat ho jama karwa dein. Shukriya!"
        return f"Assalam-o-Alaikum {customer_name}, aap ka Rs. {balance:,} udhaar kaafi din se baqaya hai. Baraye meherbani jald az jald hisab clear kar dein. Shukriya."

    prompt = f"""
    Write a short, respectful WhatsApp payment reminder in Roman Urdu from a local Pakistani kiryana shopkeeper.
    Customer Name: {customer_name}
    Outstanding Balance: Rs. {balance:,}
    Tone: {tone} ("Friendly" = warm, neighborly, polite; "Firm" = respectful but direct and urgent).
    Keep it under 35 words. Return ONLY the message text, no quotes or extra commentary.
    """
    resp = call_gemini_with_retry(
        lambda: client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.4),
        )
    )
    return resp.text.strip()


# ==============================================================================
# INITIALIZATION & SIDEBAR NAVIGATION
# ==============================================================================
init_db()

if "pending_entries" not in st.session_state:
    st.session_state.pending_entries = None
if "voice_query_answer" not in st.session_state:
    st.session_state.voice_query_answer = None
if "generated_reminder" not in st.session_state:
    st.session_state.generated_reminder = {}

with st.sidebar:
    st.title("📒 Khata Voice")
    st.caption("Bol kar udhaar likhein • Voice Digital Ledger")

    if not get_gemini_client():
        st.error("⚠️ `GEMINI_API_KEY` not found! Set it in your environment to enable AI voice/text parsing.")

    st.divider()
    page = st.radio(
        "Menu / Safha Chunein",
        [
            "🎙️ Add Entry (Naya Indraj)",
            "📊 Ledger & Dashboard (Khata)",
            "📲 Reminders (Yaad-Dihani)",
        ],
        label_visibility="collapsed",
    )

    st.divider()
    if st.button("🔄 Load Demo Data (Sample Khata)"):
        seed_demo_data()
        st.session_state.pending_entries = None
        st.session_state.voice_query_answer = None
        st.success("Demo data reloaded!")
        st.rerun()


# ==============================================================================
# PAGE 1: ADD ENTRY (VOICE / TEXT + REVIEW TABLE)
# ==============================================================================
if page == "🎙️ Add Entry (Naya Indraj)":
    st.header("🎙️ Naya Indraj Karein (Voice / Text Entry)")
    st.caption("Urdu ya Roman Urdu mein bolein ya likhein — udhaar, wapsi, ya kisi ka baqaya poochein.")

    col_voice, col_text = st.columns([1, 1], gap="large")

    with col_voice:
        st.subheader("1. Bol Kar Likhein (Record Voice)")
        audio_val = st.audio_input("Mic dabayein aur bolein (Tap mic & speak)")
        if audio_val is not None:
            if st.button("✨ Voice Process Karein (Extract from Audio)", type="primary"):
                with st.spinner("Gemini aap ki awaaz samajh raha hai..."):
                    try:
                        audio_bytes = audio_val.read()
                        mime = audio_val.type or "audio/wav"
                        result = extract_from_gemini(audio_bytes=audio_bytes, audio_mime=mime)
                        st.session_state["_last_result"] = result
                    except Exception as e:
                        st.error(
                            f"Awaaz saaf samajh nahi aayi ya API error hua ({e}). "
                            "Baraye meherbani neeche Text Box mein likh kar try karein."
                        )

    with col_text:
        st.subheader("2. Ya Likh Kar Darj Karein (Text Fallback)")
        text_val = st.text_area(
            "Urdu ya Roman Urdu mein likhein",
            placeholder="Misaal: Ahmed ko 2 kilo chawal udhaar diye 500 rupay, aur Bilal ne 300 wapis kiye",
            height=115,
        )
        if st.button("📝 Text Process Karein (Extract from Text)", type="primary"):
            if not text_val.strip():
                st.warning("Pehle kuch likhein (Please enter some text first).")
            else:
                with st.spinner("Entry nikaali ja rahi hai..."):
                    try:
                        result = extract_from_gemini(text_input=text_val.strip())
                        st.session_state["_last_result"] = result
                    except Exception as e:
                        st.error(f"Text process karne mein masla hua: {e}")

    # Process Gemini extraction result into state
    if "_last_result" in st.session_state and st.session_state["_last_result"] is not None:
        res: KhataResponse = st.session_state.pop("_last_result")
        st.session_state.voice_query_answer = None
        st.session_state.pending_entries = None

        if res.intent == "balance_query" and res.query_customer_name:
            ledger = get_ledger_df()
            match = ledger[ledger["Customer"].str.lower() == res.query_customer_name.lower()]
            if not match.empty:
                row = match.iloc[0]
                bal = int(row["Balance"])
                if bal > 0:
                    st.session_state.voice_query_answer = (
                        f"📌 **{row['Customer']}** ke zimme **Rs. {bal:,}** baqaya (udhaar) hain."
                    )
                else:
                    st.session_state.voice_query_answer = (
                        f"✅ **{row['Customer']}** ka hisab bilkul clear hai (Rs. 0 baqaya)."
                    )
            else:
                st.session_state.voice_query_answer = (
                    f"🔍 **{res.query_customer_name}** naam ka koi gahak khata mein nahi mila."
                )

        elif res.transactions:
            rows = []
            for tx in res.transactions:
                items_str = ", ".join(
                    [
                        f"{(f'{i.quantity:g} ' if i.quantity else '')}{(f'{i.unit} ' if i.unit else '')}{i.name}".strip()
                        for i in tx.items
                    ]
                )
                rows.append(
                    {
                        "Customer Name": tx.customer_name,
                        "Type": tx.type,
                        "Items": items_str,
                        "Amount (PKR)": tx.amount_pkr if tx.amount_pkr is not None else 0,
                        "Missing Amount": tx.amount_pkr is None,
                        "Confidence": round(tx.confidence, 2),
                        "Transcript": tx.transcript,
                    }
                )
            st.session_state.pending_entries = pd.DataFrame(rows)
        else:
            st.warning(
                "Koi wazeh entry nahi mili. Dubara bolein ya saamne Text Box mein likh dein."
            )

    # Display Voice Query Answer (Feature 6)
    if st.session_state.voice_query_answer:
        st.divider()
        st.info(st.session_state.voice_query_answer)

    # Display Review & Edit Table (Feature 3)
    if st.session_state.pending_entries is not None and not st.session_state.pending_entries.empty:
        st.divider()
        st.subheader("✅ Tasdeeq Karein (Review & Edit Before Saving)")
        st.caption("Koi naam ya raqam ghalat ho to table mein click kar ke theek kar lein.")

        df_pending = st.session_state.pending_entries

        # Highlight low-confidence or missing-amount rows
        needs_attention = df_pending[(df_pending["Confidence"] < 0.75) | (df_pending["Missing Amount"])]
        if not needs_attention.empty:
            st.warning(
                "⚠️ Kuch entries mein raqam (amount) missing hai ya awaaz thori kam wazeh thi — save karne se pehle check kar lein!"
            )

        def highlight_risky_rows(row):
            if row["Missing Amount"] or row["Confidence"] < 0.75:
                return ["background-color: #fff3cd; color: #664d03"] * len(row)
            return [""] * len(row)

        styled_preview = df_pending.style.apply(highlight_risky_rows, axis=1)
        st.dataframe(styled_preview, use_container_width=True, hide_index=True)

        st.markdown("**Editable Table (Yahan Tabdeeli Karein):**")
        edited_df = st.data_editor(
            df_pending[["Customer Name", "Type", "Items", "Amount (PKR)", "Confidence", "Transcript"]],
            use_container_width=True,
            num_rows="dynamic",
            column_config={
                "Customer Name": st.column_config.TextColumn("Customer Name", required=True),
                "Type": st.column_config.SelectboxColumn(
                    "Type", options=["udhaar", "payment"], required=True
                ),
                "Items": st.column_config.TextColumn("Items / Tafseel"),
                "Amount (PKR)": st.column_config.NumberColumn(
                    "Amount (PKR)", min_value=0, step=10, required=True
                ),
                "Confidence": st.column_config.ProgressColumn(
                    "AI Confidence", min_value=0.0, max_value=1.0, format="%.2f"
                ),
                "Transcript": st.column_config.TextColumn("Suna Gaya (Transcript)", disabled=True),
            },
            key="khata_editor",
        )

        col_save, col_discard = st.columns([2, 1])
        with col_save:
            if st.button("💾 Save to Khata (Khata Mein Darj Karein)", type="primary"):
                invalid_rows = edited_df[
                    (edited_df["Customer Name"].str.strip() == "") | (edited_df["Amount (PKR)"] <= 0)
                ]
                if not invalid_rows.empty:
                    st.error("Har entry ke liye Customer Name aur 0 se zyada Amount zaroori hai.")
                else:
                    for _, r in edited_df.iterrows():
                        save_transaction(
                            customer_name=str(r["Customer Name"]),
                            ttype=str(r["Type"]),
                            items_str=str(r["Items"] or ""),
                            amount=int(r["Amount (PKR)"]),
                        )
                    st.session_state.pending_entries = None
                    st.success("🎉 Tamam entries Khata mein mehfooz ho gayeen!")
                    st.rerun()
        with col_discard:
            if st.button("🗑️ Cancel / Clear"):
                st.session_state.pending_entries = None
                st.rerun()

    # Quick Example Prompts helper
    with st.expander("💡 Test karne ke liye misaalein (Example Phrases)"):
        st.markdown(
            """
            - *"Ahmed ko 2 kilo chawal udhaar diye, 500 rupay"*
            - *"Bilal ne 300 rupay wapis kiye"*
            - *"Sana baji ka 1 dozen anday aur 2 dahi, 650"*
            - *"Usman ko 1000 ka aata udhaar diya aur Tariq ne 1500 jama karwaye"*
            - *"Ahmed ka kitna baqaya hai?"* *(Voice/Text Balance Query)*
            """
        )


# ==============================================================================
# PAGE 2: LEDGER & DASHBOARD
# ==============================================================================
elif page == "📊 Ledger & Dashboard (Khata)":
    st.header("📊 Khata Dashboard & Ledger")
    st.caption("Tamam gahakon ka hisab kitab aur aaj ki karwai.")

    ledger_df = get_ledger_df()
    today_udhaar, today_paid = get_today_stats()

    total_outstanding = int(ledger_df[ledger_df["Balance"] > 0]["Balance"].sum())
    debtors_count = int((ledger_df["Balance"] > 0).sum())

    # Top KPI Metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Baqaya (Outstanding)", f"Rs. {total_outstanding:,}")
    m2.metric("Maqrooz Gahak (Customers Owe)", f"{debtors_count} Customers")
    m3.metric("Aaj Ka Udhaar (Today Credit)", f"Rs. {today_udhaar:,}")
    m4.metric("Aaj Ki Wasooli (Today Paid)", f"Rs. {today_paid:,}")

    st.divider()

    col_table, col_chart = st.columns([3, 2], gap="large")

    with col_table:
        st.subheader("📋 Gahakon Ki List (Balances Highest First)")
        st.caption(f"Laal rang (Red) = Rs. {HIGH_BALANCE_THRESHOLD:,} se zyada baqaya")

        def style_balance(val):
            if val >= HIGH_BALANCE_THRESHOLD:
                return "background-color: #f8d7da; color: #842029; font-weight: bold;"
            elif val > 0:
                return "background-color: #fff3cd; color: #664d03;"
            return "background-color: #d1e7dd; color: #0f5132;"

        display_df = ledger_df[["Customer", "Phone", "Total_Udhaar", "Total_Paid", "Balance"]].rename(
            columns={
                "Total_Udhaar": "Kul Udhaar",
                "Total_Paid": "Wasool Shuda",
                "Balance": "Baqaya (PKR)",
            }
        )
        styled_ledger = display_df.style.map(style_balance, subset=["Baqaya (PKR)"]).format(
            {"Kul Udhaar": "{:,}", "Wasool Shuda": "{:,}", "Baqaya (PKR)": "{:,}"}
        )
        st.dataframe(styled_ledger, use_container_width=True, hide_index=True)

    with col_chart:
        st.subheader("📈 Top 5 Debtors (Sabse Zyada Baqaya)")
        top5 = ledger_df[ledger_df["Balance"] > 0].head(5)
        if not top5.empty:
            chart_data = top5.set_index("Customer")[["Balance"]]
            st.bar_chart(chart_data, color="#d9534f")
        else:
            st.info("Kisi gahak ke zimme koi baqaya nahi hai!")

    # Customer Drill-down History
    st.divider()
    st.subheader("🔍 Gahak Ki Mukammal Tafseel (Customer History)")
    if not ledger_df.empty:
        selected_cust_name = st.selectbox(
            "Gahak chunein (Select Customer to view history & edit phone)",
            ledger_df["Customer"].tolist(),
        )
        cust_row = ledger_df[ledger_df["Customer"] == selected_cust_name].iloc[0]
        cid = int(cust_row["id"])

        c_info1, c_info2, c_info3 = st.columns([2, 2, 1])
        c_info1.markdown(f"**Current Balance:** `Rs. {int(cust_row['Balance']):,}`")
        new_phone = c_info2.text_input(
            "WhatsApp Number (e.g. 923001234567)",
            value=cust_row["Phone"],
            key=f"phone_{cid}",
            label_visibility="collapsed",
            placeholder="Phone: 923001234567",
        )
        if c_info3.button("Save Phone"):
            with get_conn() as conn:
                conn.execute("UPDATE customers SET phone = ? WHERE id = ?", (new_phone.strip(), cid))
                conn.commit()
            st.success("Number mehfooz!")
            st.rerun()

        hist_df = get_customer_history(cid)
        if not hist_df.empty:
            st.dataframe(hist_df, use_container_width=True, hide_index=True)
        else:
            st.info("Is gahak ka koi purana record nahi hai.")


# ==============================================================================
# PAGE 3: REMINDER GENERATOR
# ==============================================================================
elif page == "📲 Reminders (Yaad-Dihani)":
    st.header("📲 WhatsApp Yaad-Dihani (Payment Reminders)")
    st.caption("Baqaya wale gahakon ke liye AI se Roman Urdu mein polite message banayein.")

    ledger_df = get_ledger_df()
    debtors_df = ledger_df[ledger_df["Balance"] > 0]

    if debtors_df.empty:
        st.success("🎉 MashaAllah! Kisi gahak ke zimme koi baqaya nahi hai.")
    else:
        tone = st.radio(
            "Message ka Lehja Chunein (Select Tone):",
            ["Friendly", "Firm"],
            horizontal=True,
        )

        st.divider()

        for _, row in debtors_df.iterrows():
            cid = int(row["id"])
            cname = row["Customer"]
            bal = int(row["Balance"])
            phone = str(row["Phone"] or "").strip()

            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.markdown(f"### {cname}")
                c1.caption(f"Phone: {phone if phone else 'Not saved (Ledger mein add karein)'}")
                c2.metric("Baqaya", f"Rs. {bal:,}")

                with c3:
                    st.write("")
                    if st.button("✍️ Send Reminder", key=f"rem_btn_{cid}"):
                        with st.spinner("Message likha ja raha hai..."):
                            try:
                                msg = generate_reminder_text(cname, bal, tone)
                                st.session_state.generated_reminder[cid] = msg
                            except Exception as e:
                                st.error(f"Message banane mein masla hua: {e}")

                # Display generated message + copy box + WhatsApp link
                if cid in st.session_state.generated_reminder:
                    msg_text = st.session_state.generated_reminder[cid]
                    st.markdown("**Message Copy Karein (Click top-right icon to copy):**")
                    st.code(msg_text, language=None)

                    encoded_msg = urllib.parse.quote(msg_text)
                    clean_phone = "".join(filter(str.isdigit, phone))
                    if clean_phone:
                        wa_url = f"https://wa.me/{clean_phone}?text={encoded_msg}"
                    else:
                        wa_url = f"https://wa.me/?text={encoded_msg}"

                    st.link_button("🟢 Open in WhatsApp (WhatsApp Par Bhejein)", wa_url)