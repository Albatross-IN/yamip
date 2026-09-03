# WhatsApp Access Control — Usage Guide

Open WhatsApp Connector · v19.0.34.0.0 · Odoo 19 (Community & Enterprise)

This guide explains how to restrict your salespeople / agents so that:

- they can see **only their own WhatsApp conversations** (not everyone's), and
- they **cannot** log out, disconnect, or otherwise manage the WhatsApp account.

There are **three independent settings** (conversation visibility, campaign &
contact visibility, and WhatsApp-account self-service). Turn on any combination.

---

## 1. Who is an "Administrator" vs an "Agent"

The module has one permission group: **WhatsApp Administrator**.

- **WhatsApp Administrator** — full control: connect / disconnect / log out the
  account, start/stop the sidecar, open the Accounts screen and the Operations
  Dashboard, manage WhatsApp groups, see every conversation.
  (Anyone with Odoo **Settings / Administration** access is automatically a
  WhatsApp Administrator.)
- **Agent** — a normal internal user **without** the WhatsApp Administrator
  group. Can use Chats, Compose and Campaigns, but cannot touch the connection.

### How to set it

Settings → **Users & Companies → Users** → open the salesperson →

- **Untick** "WhatsApp Administrator".
- Also remove **Administration: Settings** if they have it (that implies the
  WhatsApp Administrator group).

Once a user is just an Agent, they automatically lose:

| Agent can NOT… |
| --- |
| Connect / Disconnect / **Log out** the WhatsApp account |
| Start / **Stop the sidecar** (which would break it for everyone) |
| Open the **Accounts** screen (your API keys / tokens) |
| Open the **Operations Dashboard** |
| Run diagnostics, sync templates |
| Manage WhatsApp **groups** (leave / rename / revoke invite) |

> These blocks are enforced on the server, not just hidden in the UI — they
> hold even against a crafted API call.

Agents **keep**: Messages, Compose, Status, Calls, Chats, CSAT, Campaigns, and
can pin / mute / archive their own chats.

---

## 2. Per-agent conversation visibility

### Turn it on

Settings → search **"WhatsApp"** → **WhatsApp Conversation Visibility**:

- **All agents see every WhatsApp conversation** *(default — no change)*
- **Each agent sees only conversations assigned to them** ← choose this

Click **Save**.

> Default = shared, so installing/upgrading changes nothing until you opt in.
> You can switch back to shared at any time.

### How visibility works

Every WhatsApp conversation has an **Assignee** (the owning agent). After you
switch to "own agent" mode:

| Conversation state | An agent sees it? |
| --- | --- |
| Assigned to **them** | ✅ Yes |
| **Unassigned** (shared queue) | ✅ Yes — so nothing is ever lost |
| Assigned to **someone else** | ❌ Hidden |
| A non-WhatsApp chat (DM, group, #channel, livechat) | ✅ Always visible — never affected |

- **WhatsApp Administrators** (and the "WhatsApp manager" role) always see
  **every** conversation.
- Only WhatsApp conversations are affected. Regular Discuss (direct messages,
  group chats, the #general channel, livechat) is never touched.

---

## 3. How a conversation gets assigned

### a) Automatically (new incoming chats)

While "own agent" mode is ON, each **new** incoming WhatsApp conversation is
auto-assigned to an agent:

1. If you have set up **Inbound Rules** that route a number / keyword to a
   specific agent → it goes to that agent. *(Best for real distribution.)*
2. Otherwise → the first agent in the account's **"Users to Notify"** list.

You can then reassign it (see below).

### b) Manually — the **WhatsApp → Conversations** screen

Open **WhatsApp → Conversations**. This is the central place to triage and
assign. It opens on a **Kanban board** with one column per agent (plus an
**Unassigned** column) — **drag a card onto an agent's column to assign it**.
Switch to the **List** for a denser view, or the **Form** for a single
conversation. Every view shows **Assignee**, **Triage** status, account,
number and **Last message** time, with filters **My Conversations /
Unassigned / All / Active / On Hold / Unresolved / Resolved** and group-by
**Assignee / Triage / Account**.

- **Admin / manager:** **drag a card** to another agent's Kanban column, or in
  the **List** click the **Assignee** cell and pick the agent **inline**, or
  open the conversation form and set **Assignee** there. (You can also select
  several list rows and use the **Assign to me** / **Mark Resolved** header
  buttons.) The conversation immediately appears in that agent's view and
  disappears from the others'.
- **From inside the chat:** while viewing a WhatsApp conversation in **Discuss**,
  click the **Assign to me** button in the conversation header to claim it.
- Each conversation's form also has **Mark Resolved / Reopen / Put on Hold** and
  an **Open Conversation** button that jumps straight into Discuss.
- **Changing the stage:** the status bar at the top of the form (Unassigned →
  Active → On hold → Resolved) is **clickable** — click any stage to move the
  conversation there directly (e.g. from *On hold* back to *Active* or
  *Unassigned*). You can also regroup the **Kanban** by *Triage* and drag a
  card between stage columns.

> Note: what each person sees in **Conversations** follows the same visibility
> rules — agents see only their own + unassigned; administrators see all.

### c) What agents can / cannot change

In "own agent" mode a **non-admin agent**:

- **can CLAIM** an *unassigned* conversation for themselves;
- **cannot** reassign a conversation to a different agent, or un-assign it back
  to the shared queue — only a WhatsApp Administrator can do that.

This keeps the isolation honest: an agent can't quietly hand off or expose a
conversation.

---

## 4. (Optional) Per-agent Campaign & Contact visibility

A separate, similar setting controls marketing records:

Settings → **WhatsApp** → **Campaign & Contact Visibility**:

- **Shared across company** *(default)*
- **Restricted to owner + their Sales Team**

When restricted, each agent sees only the Campaigns, Contact Lists, Broadcast
Groups, Standing Orders and Status Broadcasts they own or that belong to a
Sales Team they are a member of / lead. Assign each agent to a **Sales Team**
for the team-sharing part to work. Administrators always see everything.

---

## 5. (Optional) WhatsApp Account self-service & visibility

By default only **WhatsApp Administrators** can add, scan or manage WhatsApp
accounts, and all accounts are visible together. This optional setting lets each
salesperson connect **their own** WhatsApp number — kept private from peers and
held until an admin approves it.

### Turn it on

Settings → **WhatsApp** → **WhatsApp Account Visibility**:

- **Shared (administrators manage all WhatsApp accounts)** *(default — no change)*
- **Self-service: each user scans their own; team leaders see their team; admins
  approve & see all** ← choose this

> Default = shared, so installing/upgrading changes nothing until you opt in.

### How it works (self-service mode)

| Who | Can do |
| --- | --- |
| **Salesperson / internal user** | Go to **WhatsApp → My WhatsApp**, add an account and **scan the QR / pair their own number**. It starts **Pending** and **cannot send or receive** until approved. They see **only their own** account. |
| **Sales Team leader** | Sees (and can add/scan) every account **in their team**. (Peers on the same team do **not** see each other's accounts.) |
| **WhatsApp Administrator** | Sees **all** accounts and is the only one who can **Approve / Reject** them, start/stop the sidecar, log out, or run diagnostics. |

### The approval step

A self-added account is **disabled by default**. While it is *Pending*:

- outbound messages are refused, and
- inbound messages are dropped (ignored),

so an unauthorised number can never message through your system. An
administrator opens the account (**WhatsApp → Accounts**, filter *Pending
Approval*) and clicks **Approve** — only then does it start sending and
receiving. **Reject** keeps it blocked.

> Prerequisite: the shared sidecar must be running (an administrator starts it
> once). Salespeople can scan into it but cannot start/stop it. Assign each user
> to a **Sales Team** so the team-leader visibility works.

---

## 6. Quick setup checklist

1. **Settings → Users** → for each salesperson: untick *WhatsApp Administrator*
   and remove *Settings* access.
2. **Settings → WhatsApp** → *WhatsApp Conversation Visibility* →
   **Each agent sees only their own** → Save.
3. *(Optional)* same screen → *Campaign & Contact Visibility* →
   **Restricted to owner + their Sales Team** → Save; put agents on Sales Teams.
4. *(Optional)* same screen → *WhatsApp Account Visibility* → **Self-service** →
   Save. Then each user adds + scans their own number under **WhatsApp → My
   WhatsApp**, and an admin **Approves** it (under **WhatsApp → Accounts**)
   before it can message. Put users on Sales Teams so leaders see their team's.
5. *(Optional but recommended)* set up **Inbound Rules** so incoming numbers
   auto-route to the right agent.
6. Reassign any existing conversations to the correct agents in
   **WhatsApp → Conversations** (set the **Assignee**, or use the **Assign to
   me** button in the chat header).

That's it. Each salesperson now sees only their own conversations; the admin
sees everything and is the only one who can manage the connection.

---

## 7. Frequently asked

**Q: Will this change anything for my current users right away?**
No. Both settings ship in their "shared / everyone sees all" default. Nothing
changes until you opt in.

**Q: My app shows "WhatsApp", but the module is "Open WhatsApp Connector".**
That's correct. "Open WhatsApp Connector" is the module / marketplace name;
"WhatsApp" is just the short in-app menu label (like Odoo's own "Sales
Management" module showing as "Sales").

**Q: An agent says a new chat isn't showing up.**
New chats auto-assign only in "own agent" mode. If it landed unassigned it is
visible to everyone; if it was assigned to another agent it is hidden by
design. An admin can reassign it.

**Q: Can a manager see all chats without being a full Odoo admin?**
Yes. Give them the **WhatsApp Administrator** group only (not Settings). In
"own agent" mode they still see every WhatsApp conversation.

**Q: Does turning this on affect normal Odoo Discuss?**
No. Direct messages, group chats, #general and livechat are never affected —
only WhatsApp conversations.
