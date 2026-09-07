"""Run: uv run streamlit run frontend/app.py

The desk's face: a support chat for customers, a desk for agents. Talks to the
API over HTTP (BACKEND_URL, default http://localhost:8000) and polls it every
couple of seconds, because ADR 0001 made everything here a poll.
"""
import os
from datetime import datetime, timezone

import httpx
import streamlit as st

API = os.environ.get("BACKEND_URL", "http://localhost:8000")
POLL = "2s"
AVATAR = {"customer": ":material/person:", "agent": ":material/support_agent:"}


def api(method: str, path: str, body: dict | None = None):
    r = httpx.request(method, API + path, json=body, timeout=10)
    if r.is_error:
        try:
            detail = r.json()["detail"]
        except (ValueError, KeyError, TypeError):
            detail = r.text
        if isinstance(detail, list):  # pydantic: one entry per bad field
            detail = "; ".join(f"{e['loc'][-1]}: {e['msg']}" for e in detail)
        raise RuntimeError(detail)
    return r.json()


def waited(iso: str) -> str:
    s = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.0f}m"


def needs(ticket: dict, skill_name: dict) -> str:
    tags = ", ".join(skill_name.get(s, "?") for s in ticket["skill_ids"])
    return ("🔥 urgent · " if ticket["urgency"] == "high" else "") + tags


def transcript(ticket: dict, me: str) -> None:
    """The chat so far. Whoever is looking sees themselves as "user"."""
    def bubble(sender, text):
        box = st.chat_message("user" if sender == me else sender, avatar=AVATAR[sender])
        box.write(text)

    bubble("customer", ticket["description"] or "*no description*")
    for m in api("GET", f"/tickets/{ticket['id']}/messages"):
        bubble(m["sender"], m["body"])


# ---- customer ---------------------------------------------------------

def support():
    st.title("Bank support")
    tid = st.session_state.get("ticket_id") or st.query_params.get("ticket", "")
    if str(tid).isdigit():
        chat(int(tid))
    else:
        start()


def start():
    languages, skills = api("GET", "/languages"), api("GET", "/skills")
    with st.chat_message("agent", avatar=AVATAR["agent"]):
        st.write("Hello! Tell us who you are, then describe the problem below to start a chat.")
        name = st.text_input("Your name", max_chars=100)
        email = st.text_input("Email")
        spoken = st.multiselect("Languages you can chat in", languages,
                                format_func=lambda x: x["name"])
        topics = st.multiselect("What do you need help with?", skills,
                                format_func=lambda x: x["name"])
    if problem := st.chat_input("Describe the problem"):
        try:
            customer = api("POST", "/customers", {"name": name, "email": email})
            ticket = api("POST", "/tickets", {
                "customer_id": customer["id"],
                "language_ids": [x["id"] for x in spoken],
                "skill_ids": [s["id"] for s in topics], "description": problem})
        except RuntimeError as e:
            st.error(str(e))
            return
        st.session_state.ticket_id = ticket["id"]
        st.query_params["ticket"] = str(ticket["id"])  # survives a page refresh
        st.rerun()


@st.fragment(run_every=POLL)
def chat(tid: int):
    try:
        ticket = api("GET", f"/tickets/{tid}")
    except RuntimeError as e:  # a stale or made-up ?ticket= in the URL
        st.error(str(e))
        forget()
        st.rerun()
    st.caption(f"Ticket #{tid} · " + {
        "open": "waiting for an agent…", "in_chat": "an agent is with you",
        "closed": "this chat is closed"}[ticket["status"]])
    transcript(ticket, me="customer")
    if ticket["status"] == "closed":
        if st.button("Start a new request"):
            forget()
            st.rerun()
    elif body := st.chat_input("Type a message"):
        api("POST", f"/tickets/{tid}/messages", {"sender": "customer", "body": body})
        st.rerun(scope="fragment")


def forget():
    st.session_state.pop("ticket_id", None)
    st.query_params.pop("ticket", None)


# ---- agent desk -------------------------------------------------------

def desk():
    st.title("Agent desk")
    me = st.sidebar.selectbox("You are", api("GET", "/agents"), index=None,
                              placeholder="pick an agent", format_func=lambda a: a["name"])
    if me is None:
        st.info("Pick an agent in the sidebar.")
        return
    auto = st.sidebar.checkbox("Keep claiming",
                               help="Take the next ticket whenever you have a free seat")
    skill_name = {s["id"]: s["name"] for s in api("GET", "/skills")}
    tick(me["id"], auto, skill_name)


@st.fragment(run_every=POLL)
def tick(agent_id: int, auto: bool, skill_name: dict):
    me = api("GET", f"/agents/{agent_id}/status")
    busy = len(me["active_ticket_ids"])
    free = me["status"] == "available" and busy < me["capacity"]
    if auto and free and api("POST", f"/agents/{agent_id}/claim"):
        st.rerun(scope="fragment")

    who, toggle, take = st.columns([2, 1, 1])
    who.markdown(f"**{me['name']}** · {me['status']} · {busy}/{me['capacity']} in chat")
    flip = "unavailable" if me["status"] == "available" else "available"
    if toggle.button(f"Go {flip}"):
        api("PUT", f"/agents/{agent_id}/status", {"status": flip})
        st.rerun(scope="fragment")
    if take.button("Claim next", disabled=not free):
        if api("POST", f"/agents/{agent_id}/claim"):
            st.rerun(scope="fragment")
        st.info("Nothing claimable right now.")

    for tid in me["active_ticket_ids"]:
        t = api("GET", f"/tickets/{tid}")
        with st.container(border=True):
            st.markdown(f"**#{t['id']} {t['customer_name']}** · {needs(t, skill_name)}")
            transcript(t, me="agent")
            if body := st.chat_input("Reply", key=f"reply-{tid}"):
                api("POST", f"/tickets/{tid}/messages", {"sender": "agent", "body": body})
                st.rerun(scope="fragment")
            if st.button("Close chat", key=f"close-{tid}"):
                api("PUT", f"/tickets/{tid}/close", {"closed_by": agent_id})
                st.rerun(scope="fragment")

    st.subheader("Waiting room")
    queue = api("GET", "/queue")
    if queue:
        st.dataframe([{"#": t["id"], "Customer": t["customer_name"],
                       "Needs": needs(t, skill_name), "Waiting": waited(t["created_at"])}
                      for t in queue], hide_index=True)
    else:
        st.caption("Waiting room is empty.")


st.set_page_config(page_title="Bank support", page_icon="💬")
st.navigation([
    st.Page(support, title="Support chat", icon="💬", default=True),
    st.Page(desk, title="Agent desk", icon="🎧", url_path="desk"),
]).run()
