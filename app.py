"""
Enterprise Knowledge Base Q&A — Streamlit frontend.

Kept intentionally simple. The KB interaction is the interesting part,
not the UI framework gymnastics.
"""

import streamlit as st
import logging
import time

from config import Config
from bedrock_kb import ask
from s3_uploader import upload_file_to_s3, start_kb_sync, get_sync_status, list_kb_documents

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


st.set_page_config(
    page_title="Enterprise KB Q&A",
    page_icon="📚",
    layout="wide",
)


def check_config() -> bool:
    """Return False and show an error if critical config is missing."""
    try:
        Config.validate()
        return True
    except EnvironmentError as e:
        st.error(f"Configuration error: {e}")
        st.info("Copy `.env.example` to `.env` and fill in your AWS + Bedrock KB details.")
        return False


def render_sidebar():
    """Document management lives in the sidebar so it doesn't clutter the main Q&A flow."""
    with st.sidebar:
        st.header("Document Management")

        uploaded = st.file_uploader(
            "Upload documents to KB",
            type=["pdf", "txt", "docx", "csv"],
            accept_multiple_files=True,
            help="Files go to S3, then Bedrock indexes them. Give it a minute after upload.",
        )

        if uploaded and st.button("Upload & Sync KB", type="primary"):
            with st.spinner("Uploading..."):
                errors = []
                for f in uploaded:
                    try:
                        upload_file_to_s3(f.read(), f.name)
                        st.success(f"Uploaded: {f.name}")
                    except Exception as e:
                        errors.append(f"{f.name}: {e}")

                if errors:
                    for err in errors:
                        st.error(err)
                else:
                    try:
                        job_id = start_kb_sync()
                        st.session_state["sync_job_id"] = job_id
                        st.info("KB sync started. Check status below.")
                    except Exception as e:
                        st.warning(f"Uploads done but sync trigger failed: {e}")

        # sync status check — only show if we have a job to track
        if "sync_job_id" in st.session_state and st.session_state["sync_job_id"]:
            if st.button("Check Sync Status"):
                status = get_sync_status(st.session_state["sync_job_id"])
                st.json(status)

        st.divider()

        st.subheader("Documents in KB")
        if st.button("Refresh document list"):
            st.session_state["docs"] = list_kb_documents()

        docs = st.session_state.get("docs", [])
        if docs:
            for doc in docs:
                st.text(f"• {doc['filename']} ({doc['size_kb']} KB)")
        else:
            st.caption("Click refresh to load document list.")


def render_query_section():
    st.title("Enterprise Knowledge Base Q&A")
    st.caption("Ask questions about your internal documents. Answers are grounded in the knowledge base — not general LLM knowledge.")

    # keep chat history in session state so the conversation stays visible
    if "history" not in st.session_state:
        st.session_state["history"] = []

    # show conversation history
    for item in st.session_state["history"]:
        with st.chat_message("user"):
            st.write(item["question"])
        with st.chat_message("assistant"):
            st.write(item["answer"])
            if item.get("sources"):
                with st.expander("Sources used"):
                    for src in item["sources"]:
                        st.text(src)

    # input at the bottom
    query = st.chat_input("Ask a question about your documents...")

    if query:
        with st.chat_message("user"):
            st.write(query)

        with st.chat_message("assistant"):
            with st.spinner("Searching knowledge base..."):
                try:
                    result = ask(query)
                except Exception as e:
                    st.error(f"Query failed: {e}")
                    logger.error(f"Query error: {e}", exc_info=True)
                    return

            st.write(result["answer"])

            if result["sources"]:
                with st.expander(f"Retrieved from {len(result['sources'])} source(s)"):
                    for src in result["sources"]:
                        st.text(src)

            # show retrieved chunks for transparency — useful for debugging retrieval quality
            if result["chunks"]:
                with st.expander("Retrieved chunks (debug)"):
                    for i, chunk in enumerate(result["chunks"], 1):
                        st.markdown(f"**Chunk {i}** (score: {chunk['score']})")
                        st.text(chunk["content"][:400] + ("..." if len(chunk["content"]) > 400 else ""))
                        st.divider()

        st.session_state["history"].append({
            "question": query,
            "answer": result["answer"],
            "sources": result["sources"],
        })


def render_settings_tab():
    """Quick config check so users can verify the setup without reading logs."""
    st.subheader("Configuration")

    col1, col2 = st.columns(2)
    with col1:
        st.text_input("Knowledge Base ID", value=Config.KNOWLEDGE_BASE_ID or "not set", disabled=True)
        st.text_input("S3 Bucket", value=Config.S3_BUCKET_NAME or "not set", disabled=True)
    with col2:
        st.text_input("AWS Region", value=Config.AWS_REGION, disabled=True)
        st.text_input("Model", value=Config.BEDROCK_MODEL_ID, disabled=True)

    st.caption("Edit `.env` to change these. Restart the app after changes.")


def main():
    if not check_config():
        return

    tab_qa, tab_settings = st.tabs(["Q&A", "Settings"])

    render_sidebar()

    with tab_qa:
        render_query_section()

    with tab_settings:
        render_settings_tab()


if __name__ == "__main__":
    main()
