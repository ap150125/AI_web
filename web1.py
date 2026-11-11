
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from flask import Flask, jsonify, request
from flask_cors import CORS
from jose import JWTError, jwt
from werkzeug.security import generate_password_hash, check_password_hash
import ollama
from tavily import TavilyClient

app = Flask(__name__)
CORS(app)

# --- PATH & CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "ethan_app.db"))

SECRET_KEY = os.environ.get("SECRET_KEY", "CHANGE_THIS_BEFORE_SHARING_THIS_APP")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "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")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24
MAX_HISTORY_MESSAGES = 20

# --- DEFINE YOUR AUTHORIZED USERS HERE ---
ALLOWED_USERS = {
    "akash": "player115",
    "alice": "password123",
    "bob": "securepass456"
}

# --- DATABASE SETUP ---

@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for high-concurrency multi-device reads/writes
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (username) REFERENCES users (username) ON DELETE CASCADE
            )
        """)
        
        # Insert users only if they do not already exist
        for username, password in ALLOWED_USERS.items():
            cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
            if not cursor.fetchone():
                pw_hash = generate_password_hash(password)
                cursor.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, pw_hash)
                )
            
        conn.commit()


init_db()

# --- AUTHENTICATION HELPERS ---

def create_access_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": username, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM
    )


def get_username_from_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

# --- HISTORY & SEARCH HELPERS ---

def get_formatted_user_history(username: str):
    """Retrieves conversation history and dynamically sets user identity in system prompt."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT role, content FROM chat_history 
            WHERE username = ? 
            ORDER BY id DESC LIMIT ?
        """, (username, MAX_HISTORY_MESSAGES))
        rows = cursor.fetchall()

    rows.reverse()
    
    # Dynamically inject current user's name into system prompt
    dynamic_system_prompt = (
        "You are an assisting chatbot named ETHAN — 'Engine for Text, Heuristics, & Augmented Navigation'. "
        f"You are currently talking to the logged-in user named '{username}'. "
        "Address them by name when appropriate. Tavily web-search context will be provided when relevant. "
        "Do not invent facts."
    )
    
    formatted_messages = [{"role": "system", "content": dynamic_system_prompt}]
    for row in rows:
        formatted_messages.append({"role": row["role"], "content": row["content"]})

    return formatted_messages


def save_message(username: str, role: str, content: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO chat_history (username, role, content) 
            VALUES (?, ?, ?)
        """, (username, role, content))
        conn.commit()


def needs_search(query: str) -> bool:
    """Classifies whether a query requires live web search."""
    q_lower = query.lower()

    forced_keywords = [
        "weather", "today", "latest", "news", "current",
        "price", "score", "who won", "stock", "update", "right now"
    ]
    if any(kw in q_lower for kw in forced_keywords):
        print(" [Classifier]: SEARCH (Forced via keyword)")
        return True

    try:
        response = ollama.chat(
            model="llama3.2:3b",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict binary classifier. "
                        "Output ONLY the word SEARCH if the question requires real-time facts, current events, live prices, or weather. "
                        "Output ONLY the word NO for general knowledge, coding, definitions, or conversation. "
                        "Do not output any punctuation or additional text."
                    ),
                },
                {"role": "user", "content": query},
            ],
            options={"temperature": 0.0}
        )

        decision = response.message.content.strip().upper()
        print(f" [Classifier output]: '{decision}'")

        return "SEARCH" in decision and "NO" not in decision

    except Exception as e:
        print(f" [Classifier error]: {e}")
        return False


def search_web(query: str) -> str:
    if not TAVILY_API_KEY:
        return "Search error: TAVILY_API_KEY is not configured."
    try:
        tavily = TavilyClient(api_key=TAVILY_API_KEY)
        results = tavily.search(query=query, max_results=2)
        return "\n\n".join(
            f"{r['title']}: {r['content']}" for r in results.get("results", [])
        )
    except Exception as e:
        return f"Search error: {e}"

# --- ROUTES ---

@app.route("/", methods=["GET"])
def serve_frontend():
    html_path = os.path.join(BASE_DIR, "web.html")
    try:
        with open(html_path, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        return "<h3>Flask backend is running.</h3><p>Put web.html in the same folder as app.py.</p>", 200


@app.route("/token", methods=["POST"])
def login_for_access_token():
    data = request.get_json(silent=True) or request.form
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"detail": "Username and password are required"}), 400

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
        user = cursor.fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"detail": "Incorrect username or password"}), 401

    return jsonify({
        "access_token": create_access_token(username),
        "token_type": "bearer",
        "username": username
    })


@app.route("/chat", methods=["POST"])
def chat_endpoint():
    username = get_username_from_token()
    if not username:
        return jsonify({"detail": "Missing or invalid token"}), 401

    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"detail": "Message is required"}), 400

    messages_payload = get_formatted_user_history(username)

    if needs_search(user_message):
        web_results = search_web(user_message)
        prompt_content = (
            f"Web search results:\n{web_results}\n\nQuestion:"
            f" {user_message}\n\nAnswer based on the results above:"
        )
    else:
        prompt_content = user_message

    messages_payload.append({"role": "user", "content": prompt_content})

    try:
        response = ollama.chat(
            model="llama3.2:3b",
            messages=messages_payload
        )
        bot_reply = response.message.content
    except Exception as error:
        return jsonify({"detail": f"Could not get a response from Ollama: {error}"}), 500

    save_message(username, "user", user_message)
    save_message(username, "assistant", bot_reply)

    return jsonify({"reply": bot_reply})


@app.route("/history", methods=["GET"])
def get_user_chat_history():
    username = get_username_from_token()
    if not username:
        return jsonify({"detail": "Missing or invalid token"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT role, content, timestamp 
            FROM chat_history 
            WHERE username = ? 
            ORDER BY id ASC
        """, (username,))
        history = [dict(row) for row in cursor.fetchall()]

    return jsonify({"history": history})


@app.route("/history", methods=["DELETE"])
def clear_user_chat_history():
    username = get_username_from_token()
    if not username:
        return jsonify({"detail": "Missing or invalid token"}), 401

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_history WHERE username = ?", (username,))
        conn.commit()

    return jsonify({"message": "Chat history cleared successfully"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
