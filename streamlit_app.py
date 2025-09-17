import streamlit as st
from tabulate import tabulate

# 👇 import all your existing functions
from main import ask, run_sql

# --- Streamlit Setup ---
st.set_page_config(page_title="SQL Assistant", page_icon="🤖", layout="wide")

if "history" not in st.session_state:
    st.session_state.history = []
if "current_sql" not in st.session_state:
    st.session_state.current_sql = None

st.title("💬 SQL Assistant (Gemini + MySQL RDS)")

# --- Chat Input ---
user_input = st.chat_input("Ask me an SQL question (type 'exit' or 'quit' to stop)")

if user_input:
    if user_input.strip().lower() in ["exit", "quit"]:
        st.warning("👋 Chat ended. Restart the app to begin again.")
    else:
        sql, tables_used = ask(user_input)
        results = run_sql(sql)

        # Save to history
        st.session_state.history.append({
            "user": user_input,
            "sql": sql,
            "tables": tables_used,
            "results": results
        })

# --- Display Chat History ---
for chat in st.session_state.history:
    with st.chat_message("user"):
        st.write(chat["user"])
    with st.chat_message("assistant"):
        st.markdown(f"**Generated SQL:**\n```sql\n{chat['sql']}\n```")
        st.write("📂 **Tables used:**", chat["tables"])
        if chat["results"]:
            st.dataframe(chat["results"])
        else:
            st.info("⚠️ No results or query failed.")
