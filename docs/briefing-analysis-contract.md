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

As of the documentation review on 2026-11-05, the [KRX KOSPI trading-hours
page](https://global.krx.co.kr/contents/GLB/06/0602/0602010201/GLB0602010201T1.jsp)
lists a 09:00–15:30 regular session and an after-market session from 16:00 to
20:00. [NXT's trading-system page](https://www.nextrade.co.kr/menu/transactionSys.do)
lists its pre-market as 08:00–08:50 and its after-market trading as
15:40–20:00. Thus 08:20 can already be within NXT pre-market, and 16:10 can be
within both venues' after-market trading; neither implies that the quoted
instrument or provider covers that venue. These general hours are not a
substitute for a current holiday, special-session or instrument-eligibility
feed. All times in this paragraph are Asia/Seoul.

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

## Consumer procedure

For each explicitly authorized portfolio, call `get_stock_analysis_context`
once per run. For a manual stock question include the requested KRX symbol;
for a briefing leave `symbols` empty to include detected holdings. Treat the
returned direct broker observation separately from older checkpoint facts;
it may be unavailable for accounts without a provider. Check each response's
source timestamp, status, completeness and gaps. A missing, stale, failed,
truncated or partial input must not be presented as a current complete fact.
Account impact remains limited until per-account freshness, available cash,
currency and complete valuation are independently established.

Investigate public company disclosures and market news independently using
only public security identifiers and event terms. For candidate discovery,
state the actual exchange universe, its coverage and observation time, the
screening thresholds for liquidity, the event window and ranking method before
selecting up to three candidates. Exclude holdings when seeking new candidates.
If a market-wide universe, liquidity, event or source cannot be checked, report
that gap and do not substitute a watchlist as the market. Separate price
observations from hypotheses; give short- and longer-horizon evidence,
conditions and invalidation, and never imply that advice authorizes orders.

`quote_status` is conservative: every symbol must carry its own current source
timestamp. The provider's retrieval time does not establish quote freshness.
Moving averages are calculated only for ordered, dated, current complete daily
series; a daily bar may still be unfinished. Session is `unknown` until a
verified calendar source and venue coverage are connected. Never interpret a
missing previous briefing as evidence that nothing changed.

Before enabling recurring runs, integrate the approved history path and obtain
verified calendar and market-wide discovery sources. Test repeat runs, stale
sources, partial failures and calendar exceptions. Verify a manual run in the target
ChatGPT project and then verify unattended access and actual receipt. The local
Codex scheduler is a different execution surface and is not evidence that a web
ChatGPT task can access this account's custom MCP connection.
