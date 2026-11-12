---
name: request-scope
description: Get authorization to touch files that a project rule protects, in one pass — and verify the boundary changed exactly as intended. Use when a tool call is denied by a boundary, when AGENTS.md or opencode.jsonc forbids an edit you need to make, when the user grants a temporary exception to a rule, or when you are about to edit a settings/permissions file.
---

# Request scope

A project's rules live in a **file**. The user's consent lives in a
**conversation**. When those two disagree, you cannot tell which wins, and the
user is forced to debug your permission model instead of doing their work.

This skill makes that mismatch resolvable in **one pass**, and makes the
boundary change itself verifiable.

## When to use

- A tool call was denied and the reason mentions a boundary or a project rule.
- A rule file (`AGENTS.md`, `opencode.jsonc`, a lint config)
  forbids an edit you need to make.
- The user says "go ahead" but the rule that blocks you is still in the file.
- The user has just edited a rule file to unblock you.
- You are about to edit a permissions or rules file.

## Rule 0 — never work around a denial

If a tool call was denied, **do not reach for an equivalent tool to achieve the
same effect.** Using `edit` because `write` was denied, or a shell wrapper
because a command was denied, is permission laundering: it defeats the control
without removing it, and it destroys the user's ability to trust any of your
other claims about what you did.

Stopping and reporting costs one round trip. Working around costs the
relationship.

## Step 1 — classify the blocker before responding

Three different blockers need three different responses. Do not treat them alike.

| Blocker | How it presents | Your move |
|---|---|---|
| **Harness denial** | Tool returns a denial / classifier reason | Report it verbatim. Do not retry the same tool. The fix is the user changing settings or rules. |
| **Project rule** | A line in `AGENTS.md` / `opencode.jsonc` / a config forbids the action | Steps 2–3. The rule must change, not the wording of your request. |
| **Missing capability** | Not a denial — a tool simply isn't installed (`make`, `pytest`) | Say so plainly. Offer an alternative path. Never present an unverified result as verified. |

A verbal "yes" **does not** override a rule in a file. If the rule is still
there, you are still blocked. Say that instead of trying and failing.

## Step 2 — make the boundary edit yourself, when you can

Before asking the user to edit a rule file, check: **is that file itself
protected?**

- Editing `AGENTS.md`, `opencode.jsonc`, or a settings file is usually *not*
  protected (those files govern you; they are not the protected assets).
- Editing files the rule protects *is* protected.

If the boundary file is not protected, **offer to make the edit yourself.** Give
the exact diff you would apply and ask only for a yes.

This removes the dominant failure mode: a user hand-editing a rule file gets it
subtly wrong (comments a line out instead of deleting it; deletes one line too
many). Those mistakes are invisible until they cause a denial or, worse, a
silent loss of a rule you needed.

## Step 3 — write one complete request, not a conversation

Ask once, in one block. Every element below, every time.

```
**Blocked by:** <file:line, quoted exactly as it appears>
**I want to change:** <paths, and whether each is currently protected>
**Narrowest unblock:** <the smallest edit to the rule that unblocks this>
**What stays closed:** <boundaries that must NOT be touched, named explicitly>
**Afterwards I will:** <concrete actions>
**Verification:** <how you will check it — or "cannot verify, here is why">
**Reversible:** <yes/no — and if temporary, what restores the original>
**Shall I make this rule edit myself?** <yes/no + the exact diff>
```

Two things this block buys you:

- **"Narrowest unblock"** stops a fix for one file becoming a blanket removal.
  If you need a data-layer file, ask for the data layer — not for all of `src/`.
- **"What stays closed"** is the part users forget to specify. Name the modules
  that genuinely are safety boundaries (limits, order execution, risk config)
  and say they must remain protected. Do not leave this to inference.

## Step 4 — verify the diff, not the state

After any boundary edit — the user's or your own — **re-read the file and
compare against what you recorded before.** Check that:

1. The intended rule changed.
2. **Nothing else changed.**

Report every unintended difference explicitly and loudly. A rule that vanished
without being asked for is a **safety regression**, not a cleanup. It matters
more than the rule you wanted gone, because nobody chose it.

Record the boundary file's prior state *before* requesting the change, so this
comparison is possible. Without a recorded "before", you can only see the
after — and you will miss collateral damage.

### Worked example (real, from this repository)

Requested: remove the rule forbidding edits to `src/` and `config/`.
The user edited the file. A naive check — "is that line gone?" — passes.

Re-reading and diffing showed a **second rule had also been removed**:

```
- Call ONLY the CLI commands from the table; do not read raw API responses.
```

That rule governs whether the executor can be bypassed. Its removal was
unintended and silent. It was caught only because the diff was checked instead
of the state. **This is the check that earns its keep.**

## Step 5 — do the work, then close the debt

- **Separate the boundary change from the work.** Commit the rule edit on its
  own so it is auditable. Bundling a permissions change into a feature commit
  hides it from anyone reviewing history.
- **If the unblock was temporary, record the restore.** Quote the exact original
  text so it can be put back verbatim, and say when it should be restored. A
  boundary that was "temporarily" opened tends to stay open.
- **State what remains unverified.** If you could not run tests, say so in the
  result, not only in passing. Never let "done" imply "checked".
- **Note the maintenance debt.** Code you were only allowed to write — not
  maintain — is code that breaks later with nobody permitted to fix it. Say that
  out loud when the grant is one-off.

## Anti-patterns

- Asking in prose across several turns, leaving the user to infer what to change.
- Treating a verbal "yes" as sufficient while the rule still sits in the file.
- Retrying a denied tool, or an equivalent tool, without telling the user.
- Requesting a broad unblock when a narrow one suffices.
- Accepting a boundary edit without diffing it.
- Leaving a "temporary" grant with no recorded restore text.
