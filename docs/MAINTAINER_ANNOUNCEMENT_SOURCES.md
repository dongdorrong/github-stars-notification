# Trusted maintainer announcement sources

Announcement collection is **off by default**. Each project must explicitly
enable `signals.announcement` and the source-specific `discussions`, `issues`,
or `rss` boolean in `config/projects.yaml`, and configure a category, label
allowlist, or official HTTPS feed. General repository content is not presumed
official. Each collector implements the page-atomic `CollectedPage` contract:
the caller persists normalized events and the proposed cursor in the same
successful transaction. An exception must not advance a cursor. One source's
failure should degrade only that source, not Release or GHSA collection.

## GitHub Discussions

`DiscussionCollector` uses one fixed read-only GraphQL `query` and no mutation.
It requests `repository.discussions` ordered by `UPDATED_AT DESC` with cursor
pagination (maximum 100 per page), and includes only explicitly configured
category names. GraphQL does not document a server-side updated-since filter
for this connection; it stops at a durable watermark with one-hour overlap by
default. It preserves the Discussion node/database ID, category, author login,
author association, created/updated timestamps and official URL. Identity is
`github:discussion:<node-id>`; trust score **90** comes from the configured
official category, not from AI. Unconfigured or disabled sources do no network
work. Event visibility is supplied from starred inventory and defaults to
`unknown`, never automatically public merely because the endpoint is readable.
The first proposed cursor carries `bootstrap_cutoff` for initial backlog
suppression. GraphQL access failures are capability-degraded, not permissions for
falling back to broader unconfigured sources.
`hasNextPage: true` requires a nonempty, changed `endCursor`; a malformed
GraphQL page cannot advance the watermark.

## Labeled GitHub Issues

`IssueCollector` uses `GET /repos/{owner}/{repo}/issues` with `since`,
`sort=updated`, descending order, Link pagination and an overlapped durable
watermark. It excludes PRs returned by the Issues endpoint. Only an explicit
allowlisted label without a denylisted label is collected. A matching
`OWNER`, `MEMBER`, or `COLLABORATOR` Issue is a notification candidate with
trust **85**; a matching unverified contributor Issue is retained with trust
**30** but **not** a candidate. General unlabeled Issues and comments are not
collected. Identity is `github:issue:<repository-id>:<number>`; repository ID
comes from the inventory, not a mutable name. Labels, association, author,
timestamps and source URL are retained. Visibility comes from inventory and
defaults to `unknown`; AI cannot raise trust.
Persisted and returned REST Link cursors must preserve the exact `since`,
`sort=updated`, `direction=desc`, `state=all`, and `per_page` filters, with only
a positive page number changing. Extra or missing query parameters fail closed
before any cursor advancement.

## Official RSS/Atom

`RSSCollector` accepts only configured official HTTPS feeds. `SafeRSSReader`
rejects credentials in URLs, non-443 ports, loopback/private/link-local/non-
global DNS answers, unsafe redirect targets, >1 MB bodies, unexpected content
types, invalid XML, and DTD/entity declarations. It validates every DNS
answer and pins the selected public IP for TLS connection, preserving hostname
certificate validation to prevent DNS-rebinding between validation and connect.
The same hard POSIX wall-clock deadline covers DNS resolution, TCP/TLS and
body read; a socket idle timeout alone is not the runtime budget. The XML
parser rejects DTD after decoding, including UTF-16 declarations that bypass
a raw-byte text check.
Configured feed URLs with query strings are rejected so embedded credentials
cannot enter the registry; item links have query strings removed before use.
At most three redirects are followed, and every destination host must be the
original configured host or be explicitly listed as `redirect_hosts` in that
feed policy. No browser, JavaScript, external entity, or shell is run.

The feed uses ETag/Last-Modified on completed scans; a nonterminal item page
is re-fetched with overlap, and no HTTP cache validator is advanced before
its last page. RSS/Atom item identity is
`rss:sha256(configured_feed_url + NUL + stable_guid_or_item_url)`; title is
never the key. Item links outside allowlisted hosts are replaced with the
official feed URL before downstream formatting. Official feed trust is **95**.
Changes to the same GUID alter `content_hash` so the event-store revision
layer can retain an update without creating a second identity. A feed failure
does not advance its cursor or HTTP validators.
RSS event visibility defaults to `unknown`; set the configured source
`visibility: public` only for a reviewed public official feed. HTTPS alone does
not prove an item or mapped project is public.
Feature-branch preview's `public_only` filter skips nonpublic feeds without
renumbering configured source indices. A durable `feed_index` from commit mode
therefore always refers to the same original feed; preview changes only its
in-memory cursor and does not fetch a private feed or save production state.

## API and permissions

The GitHub REST adapter pins `X-GitHub-Api-Version: 2022-11-28` and only uses
read endpoints. Public Issues can be read anonymously, but fine-grained PATs
need **Issues: read** for access-controlled resources. Discussions require
GraphQL authentication; GitHub's documented classic scopes are `public_repo`
for public and `repo` for private. Fine-grained Discussion capability must be
tested and may degrade cleanly. A 403 may also indicate rate limiting; use
safe status/header categories, never raw bodies or tokens. REST Issues use
`Link rel=next`; GraphQL uses `pageInfo.endCursor`/`hasNextPage`.

Official references: [REST Issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28),
[GraphQL Discussions](https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions),
[GitHub API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

## Rollout and verification

Feature-branch preview may query only explicitly opted-in **public** sources;
it must never save the DB or send Slack. Default `shadow` mode may persist new
signals but must not deliver them. A later operator-approved canary/full mode
is required before production announcement Slack. Inspect sanitized counts,
source errors, trust reasons and mappings before promotion. Disable a source by
turning off its signal flag; retained event/revision rows remain auditable.

Token-free fixtures:

```bash
python3 -m unittest discover -s tests -p test_announcements.py -v
```
