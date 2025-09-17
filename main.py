import os
import re
import pickle
import asyncio
from sqlalchemy import text
from urllib.parse import quote_plus
from tabulate import tabulate

from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain.prompts import ChatPromptTemplate
from langchain_community.vectorstores import FAISS
from langchain_community.docstore.document import Document
from langchain_community.utilities import SQLDatabase

# --- Setup ---
os.environ["GOOGLE_API_KEY"] = os.getenv(
    "GOOGLE_API_KEY",
    "API KEY"
)

# MySQL RDS
password = quote_plus()
db_uri = f""
db = SQLDatabase.from_uri(db_uri)

# Extract schema
schema_text = db.get_table_info()
schema_text_clean = re.sub(r"/\*.*?\*/", "", schema_text, flags=re.S)
tables = schema_text_clean.split("\n\n\n")

table_docs = []
for t in tables:
    match = re.search(r"CREATE TABLE\s+`?(\w+)`?", t, re.IGNORECASE)
    if match:
        table_name = match.group(1)
        doc = Document(page_content=t.strip(), metadata={"table": table_name})
        table_docs.append(doc)

print(f"📚 Found {len(table_docs)} tables in schema")

# --- Embeddings + FAISS Setup (for schema retrieval only) ---
# try:
#     embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
#     table_index = FAISS.from_documents(table_docs, embeddings)
#     print("✅ Embeddings + FAISS index built successfully (for schema)")
# except Exception as e:
#     print(f"⚠️ Embeddings initialization failed: {e}")
#     embeddings = None
#     table_index = None

try:
    # Ensure an event loop exists (Streamlit runs in a different thread)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    embeddings = GoogleGenerativeAIEmbeddings(model="models/text-embedding-004")
    table_index = FAISS.from_documents(table_docs, embeddings)
    print("✅ Embeddings + FAISS index built successfully (for schema)")

except Exception as e:
    print(f"⚠️ Embeddings initialization failed: {e}")
    embeddings = None
    table_index = None

# --- Persistent Exact Cache Setup ---
CACHE_DIR = "cache_data"
os.makedirs(CACHE_DIR, exist_ok=True)

CACHE_FILE = os.path.join(CACHE_DIR, "query_cache.pkl")

if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, "rb") as f:
        query_cache = pickle.load(f)
else:
    query_cache = {}

# --- Prompts ---
base_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are an expert SQL assistant. "
               "Use ONLY the provided schema info to write correct SQL queries. "
               "Always return the SQL query only."),
    ("system", "Schema info:\n{context}"),
    ("human", "User question: {original_question}\nGenerate SQL for it."),
])

refine_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are an expert SQL assistant. You will refine or update the existing SQL query "
               "based on the user’s new request. Always return the updated SQL query only."),
    ("system", "Schema info:\n{context}"),
    ("human", "Current SQL:\n{current_sql}\n\nUser refinement: {user_request}\n\nNow update the SQL accordingly."),
])

# --- LLM ---
llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)

# --- State ---
history = []
current_sql = None

# --- Cache Lookup ---
def cache_lookup(query: str):
    if query in query_cache:
        print("🔄 Serving from Exact-Cache")
        return query_cache[query], "Exact-Cache"
    return None, None

def cache_add(query: str, sql: str):
    query_cache[query] = sql
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(query_cache, f)

# --- Schema Retrieval ---
def find_relevant_schema(user_query, threshold=0.60, top_k=5):
    if not table_index:
        return "", []
    
    results = table_index.similarity_search_with_score(user_query, k=top_k)
    
    filtered = []
    table_names = []
    for doc, score in results:
        similarity = 1 / (1 + score)  # Convert L2 distance to similarity
        if similarity >= threshold:
            filtered.append(doc.page_content)
            table_names.append(doc.metadata.get("table"))
            print(f"✅ Schema kept: {doc.metadata.get('table')} (similarity={similarity:.2f})")
        else:
            print(f"❌ Schema skipped: {doc.metadata.get('table')} (similarity={similarity:.2f})")
    
    return "\n\n".join(filtered), table_names

def clean_sql(sql: str) -> str:
    if not sql:
        return sql
    sql = sql.strip()
    sql = re.sub(r"^```sql\s*", "", sql, flags=re.IGNORECASE | re.MULTILINE)
    sql = re.sub(r"```$", "", sql, flags=re.MULTILINE)
    return sql.strip()

def run_sql(sql: str, limit: int = 20):
    if not sql:
        return None

    sql = clean_sql(sql)

    try:
        with db._engine.connect() as conn:
            result = conn.execute(text(sql))
            rows = result.fetchmany(limit)
            columns = result.keys()
            data = [dict(zip(columns, row)) for row in rows]
            
            if data:
                print(tabulate(data, headers="keys", tablefmt="grid"))
            else:
                print("⚠️ No results returned")

            return data

    except Exception as e:
        print(f"⚠️ SQL execution failed: {e}\n[SQL: {sql}]")
        return None

# --- Main Ask Function ---
def ask(query: str):
    global current_sql

    context, tables_used = find_relevant_schema(query, threshold=0.60 , top_k=5)
    sql, source = None, None

    cached, cache_source = cache_lookup(query)
    if cached:
        sql = cached
        source = cache_source
    else:
        try:
            if current_sql is None:
                result = (base_prompt | llm).invoke({
                    "original_question": query,
                    "context": context
                })
            else:
                result = (refine_prompt | llm).invoke({
                    "current_sql": current_sql,
                    "user_request": query,
                    "context": context
                })

            sql = result.content if hasattr(result, "content") else str(result)
            source = "Gemini"

            cache_add(query, sql)

        except Exception as e:
            print(f"⚠️ Gemini API failed: {e}")
            fallback_sql, fb_source = cache_lookup(query)
            if fallback_sql:
                sql = fallback_sql
                source = f"Cache-Fallback via {fb_source}"
            else:
                sql = None
                source = "Error (no API, no cache)"

    current_sql = sql
    history.append({
        "original_prompt": query,
        "sql_generated": sql,
        "tables_used": tables_used,
        "source": source
    })
    return sql, tables_used

# --- Interactive Chat Loop ---
def chat_loop():
    print("\n💬 SQL Assistant Chat (type 'exit' to quit)\n")
    while True:
        user_query = input("You: ")
        if user_query.strip().lower() in ["exit", "quit"]:
            print("👋 Exiting chat. Bye!")
            break

        sql, tables_used = ask(user_query)
        print("\n🤖 SQL Generated:\n", sql)
        print("📂 Tables used:", tables_used)

        results = run_sql(sql)
        if results:
            print("✅ Query executed successfully.\n")
        else:
            print("⚠️ No results or query failed.\n")

if __name__ == "__main__":
    chat_loop()
