# ethereum/EIPs submission checklist

How to take `eips/draft-native-eth-a2a-escrow.md` from this repo to a numbered, merged
Draft EIP in [`ethereum/EIPs`](https://github.com/ethereum/EIPs). Follow [EIP-1] for the
authoritative process; this is the condensed operational path.

[EIP-1]: https://eips.ethereum.org/EIPS/eip-1

---

## 0. Preconditions (do these first)

- [ ] **GitHub handles in `author:` are real and resolve.** EIP-1 requires every `@handle` to be a
      valid GitHub account. Confirm `@abhicris`, `@Pattermesh`, `@kcolbchain` all exist. If an author
      prefers name-only, use `Name <email>` form instead — but at least one author must be reachable.
- [ ] **Open the ethereum-magicians thread** (see `eips/magicians-post-draft.md`) and let it gather a
      little discussion. The EIP bot/editors expect a real `discussions-to:` URL.
- [ ] **Reference implementation is public and stable.** `contracts/AgentEscrow.sol` must be reachable
      at a permanent URL. EIP body links into this repo are fine, but editors prefer the spec be
      self-contained — the normative interface lives in the EIP text, not only in the repo.
- [ ] **Close the two known conformance gaps** (or keep them as clearly-flagged TODOs in the
      Reference Implementation section, which the current draft does): add ERC-165 `supportsInterface`
      to the reference contract and a `test_supportsInterface` asserting `0x5c3738e9`.

## 1. Fork and branch

- [ ] Fork [`ethereum/EIPs`](https://github.com/ethereum/EIPs) to your account.
- [ ] `git clone` your fork; `git checkout -b eip-native-eth-a2a-escrow`.
- [ ] Read `ethereum/EIPs`'s `CONTRIBUTING` and the EIP template once more — the repo CI
      (`eipw` linter + HTML preview build) is strict and will reject on format alone.

## 2. File naming and number

- [ ] **Do NOT pick your own number.** New EIPs are submitted as `EIPS/eip-draft_<slug>.md` or you
      may use a placeholder; an **EIP editor assigns the number** during review. In practice the
      common path is: name the file `EIPS/eip-XXXX.md` (literal `XXXX`) and set `eip: XXXX`, or use a
      descriptive `eip-draft_native_eth_a2a_escrow.md`. The editor renames it to the assigned number
      (`eip-7xxx.md`) on merge.
- [ ] Copy `eips/draft-native-eth-a2a-escrow.md` into `EIPS/` in the fork under that name.
- [ ] Place asset files (diagrams, if any are externalized) under `assets/eip-XXXX/`. The current
      draft uses only inline ASCII, so no assets are needed.

## 3. Frontmatter cleanup (the two placeholders)

- [ ] Set `discussions-to:` to the actual ethereum-magicians topic URL.
- [ ] Set `eip:` to match the filename. If using the `XXXX` placeholder, leave both as `XXXX`; the
      editor fills them. **Remove the `EDITOR NOTE` HTML comment block** at the top of the draft.
- [ ] Confirm the rest of the frontmatter is exactly the EIP-1 set in the right order:
      `eip, title, description, author, discussions-to, status, type, category, created, requires`.
      - `status: Draft`
      - `type: Standards Track`
      - `category: ERC`
      - `requires: 165`
      - `title` ≤ 44 chars, `description` ≤ 140 chars, neither ending in a period, neither
        repeating the word "standard"/"EIP" (eipw enforces these).

## 4. Body conformance (what the linter and editors check)

- [ ] Required sections present and in EIP-1 order: **Abstract, Motivation, Specification, Rationale,
      Backwards Compatibility, Reference Implementation, Security Considerations, Copyright**
      (plus optional Test Cases — present here). All eight required headers are `##` level.
- [ ] **RFC-2119 keywords** boilerplate paragraph present in Specification (it is).
- [ ] **Copyright** is exactly the CC0 waiver line EIP-1 mandates (it is).
- [ ] Internal links to other EIPs use the relative `./eip-N.md` form (the draft links ERC-165 as
      `./eip-165.md` — correct).
- [ ] No broken external links; `eipw` checks reachability of some.
- [ ] Markdown passes the repo's `markdownlint` config (line length is not capped, but tables and
      headers must be well-formed).

## 5. Open the PR

- [ ] Push the branch to your fork; open a PR into `ethereum/EIPs:master`.
- [ ] PR title convention: `Add EIP: Native-ETH Agent-to-Agent Escrow` (or
      `Add ERC: ...`). The bot comments with the assigned number and lint results within minutes.
- [ ] Address every `eipw` / preview-CI failure. Re-push; CI re-runs.
- [ ] An **EIP editor** reviews for format + EIP-1 conformance (NOT for whether the idea is good —
      that's the magicians thread's job). Once it conforms, an editor assigns the number, may rename
      the file, and merges it as **Draft**.

## 6. After merge

- [ ] Update the `discussions-to` thread title and this repo's `eips/draft-native-eth-a2a-escrow.md`
      frontmatter to reference the assigned `ERC-XXXX` number, and link the merged EIP from issue #50
      and PR #51.
- [ ] Advancing **Draft → Review → Last Call → Final** is a later, separate process driven by author
      readiness + editor sign-off + the discussion thread; it is out of scope for initial submission.

---

### Quick reference: what an editor will and won't gate on

| Editor gates on (must fix to merge) | Editor does NOT gate on |
|---|---|
| Frontmatter format, field order, char limits | Whether the primitive is a good idea |
| All required sections present, correct order | Whether the community agrees with the design |
| Valid author handles, real `discussions-to` | Adoption / deployment count |
| CC0 copyright line verbatim | Test coverage depth |
| Relative EIP links, lint pass | The bikeshed in §Rationale |
