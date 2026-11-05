# Korean market analysis and briefing: implementation boundary

Status: **partial implementation** for [issue #30](https://github.com/johneybi/InvestKitchen/issues/30).
The `get_stock_analysis_context` MCP read composes scoped Portfolio, current
Policy and Plan, bounded broker-account reads when supported, quotes and daily
bars. It is shared evidence for manual analysis and briefings; it does not
generate recommendations or initiate execution. It calls each source again on
each request. The output explicitly marks account impact as limited and does
not infer calendar, discovery, news or prior-briefing data.

The private history module provides bounded records, trusted per-record
approval verification, scoped reads and idempotent run IDs. It is **not exposed
as an MCP endpoint** and is not wired to recurring tasks. A production workflow
needs its own reviewed server-side standing authorization design before any
unattended history write. A briefing record is not a Decision or approval to
trade. No result in a conversation is silently persisted.

Both manual analysis and scheduled briefings must use the same data
contract. Each input needs its source, observation time, market/venue and
coverage, plus an explicit fresh, stale, missing, partial or failed status.
Account state, cash, policy and active plans must be read on every run; a failed
read must never be replaced by conversation memory. Account scopes and currency
must remain separate, and aggregate weights require complete, consistently
timed valuation and FX inputs. Brokerage cash coverage must not be presented as
available-to-trade unless the source actually supports that interpretation.

A market calendar must establish holidays, special sessions and KRX regular
hours separately from after-hours and alternative trading venues. No weekday or
fixed clock assumption is sufficient. The proposed 08:20 and 16:10 Asia/Seoul
times are defaults for evaluation, not verified market boundaries or confirmed
user preferences; 16:10 does not imply all trading has ended.

The existing symbol quote/OHLCV reads do not discover a market-wide candidate
universe. A production implementation still needs an attributed universe,
liquidity and event data, coverage rules, bounded selection, public disclosure
and news sources, plus fail-closed handling when those inputs are unavailable.
Public research must not include private account contents in external queries.

The result should contain at most three priorities, holdings with evidence,
conditions, invalidation and account impact, at most three new candidates,
changes from the prior briefing, and source times and gaps. Horizons must be
explicit. A lack of changes is a valid result; a missing previous briefing is
not evidence of no change. Analysis is Advice, not a Decision, transaction,
approval receipt or authorization to execute.

Before enabling recurring runs, integrate the approved history path and obtain
verified calendar and market-wide discovery sources. Test repeat runs, stale
sources, partial failures and calendar exceptions. Verify a manual run in the target
ChatGPT project and then verify unattended access and actual receipt. The local
Codex scheduler is a different execution surface and is not evidence that a web
ChatGPT task can access this account's custom MCP connection.
