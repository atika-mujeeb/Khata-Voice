Khata Voice 🎙️📒
A voice-powered digital ledger for Pakistani kiryana shops. Shopkeepers speak in Urdu or Roman Urdu, and the app logs udhaar (credit) and payments automatically.

Built at MLH Hack Day @ SZABIST Karachi (AWS Student Builder Group, Google Gemini).

Features
Voice or text entry in Urdu / Roman Urdu
Gemini extracts customer, items, amount and type (udhaar or payment)
Review and edit entries before saving
Dashboard of who owes what
Polite Roman Urdu payment reminders
Tech stack
Python, Streamlit, Google Gemini API, SQLite

Run it locally
Get a free API key from https://aistudio.google.com/app/apikey
Install dependencies: pip install -r requirements.txt
Set your key (PowerShell): $env:GEMINI_API_KEY="your_key"
Run: python -m streamlit run app.py
Try these phrases
Ahmed ko 2 kilo chawal udhaar diye, 500 rupay
Bilal ne 300 rupay wapis kiye
Ahmed ka kitna baqaya hai?
Notes
The free Gemini tier has daily request limits per model.

Team
[Furqan-Ul-Haq] and [Atika Mujeeb]
