# Korean market analysis and briefing: implementation boundary

Status: **partial server implementation**, with a separate read-only ChatGPT
scheduled workflow, for [issue #30](https://github.com/johneybi/InvestKitchen/issues/30).
The server changes below are not deployed merely because a ChatGPT task works
against the already deployed individual read tools.
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

Unattended writes require an approved history authorization path. They are not a
prerequisite for a read-only briefing that explicitly reports prior-briefing
comparison as unavailable. Test repeat runs, stale sources, partial failures and
calendar exceptions. Verify manual reads and unattended access independently.
The local Codex scheduler is a different execution surface and is not evidence
that a web ChatGPT task can access a custom MCP connection.

## Read-only ChatGPT scheduled workflow

A first usable workflow can run against existing deployed tools without deploying
the new aggregate endpoint or granting any background write authority:

1. Discover authorized portfolios with `list_portfolios`. Read each portfolio,
   its current policy and active plans on every run. Discover broker account IDs
   from the portfolio response and call `get_account_state(refresh=true)` when
   available. Never substitute prior chat contents for a failed read.
2. Read quotes and daily bars for domestic holdings in bounded batches. Preserve
   per-source times, partial coverage and currency boundaries. Complete holdings
   with incomplete cash still support bounded holding analysis, not invented
   account-wide weights or order quantities.
3. Independently research exchange notices, public market rankings, disclosures
   and news. State actual coverage: a public ranking is not an exhaustive market
   scan. If event evidence or liquidity cannot be checked, omit new candidates.
   Keep private account content out of public queries.
4. Return at most three priorities, holding-specific conditional advice, at most
   three researched new candidates, public citations and material data gaps.
   Compare with current InvestKitchen plans where possible; mark unavailable
   briefing history explicitly. Do not write decisions, transactions or records.
5. Store the complete procedure in each scheduled task's instructions. Start
   each run in a fresh chat so project memory is not a hidden dependency. Results
   remain in ChatGPT's own run history; this is not InvestKitchen history storage.

The initial recurring configuration uses weekdays at 08:15 and 16:15 in
`Asia/Seoul`, matching the web editor's offered 15-minute time slots. Weekday
scheduling is not a trading calendar: each run checks holidays and special
sessions, or labels the session unverified. A holiday run returns a short notice.
The afternoon report is after the regular session, not necessarily after all
venues have closed. An off-schedule manual test labels its real execution time.

### Verification and recovery

Keep these states separate: saved task, manual Run now, clock-triggered read,
complete briefing generation, recurring-session delivery, and backend deployment.
An observed one-time clock-triggered run on 2026-11-05 successfully used the
existing InvestKitchen reads and reported partial data coverage. This verifies
that tested connection, not every future run or the proposed aggregate endpoint.
A separately started full briefing also completed against the existing server:
it read current broker observations, policy and plans, quotes and daily bars,
researched public disclosures/news, and reported unavailable account bindings,
incomplete cash and conflicting price sources. The two recurring schedules are
registered; their future regular-session runs remain distinct from this manual
test. No server code was deployed as part of scheduling. The ChatGPT project
instructions were also extended, preserving their existing operating contract,
so manual stock questions use the same fresh-read and conditional-advice rules.

The web UI can transiently show an unreadable response while a run is loading.
Reopen the existing run from Scheduled or use its reload-response control before
declaring a provider failure. Inspect the actual completed result and source
times; a scheduled task row or a spinner is not evidence of successful analysis.
Do not click Run now repeatedly to recover rendering, because that starts new
work. Preserve existing jobs and avoid duplicate recurring schedules.

If a source is unavailable, retry once, report the affected analysis as limited,
and continue unaffected reads. If InvestKitchen is wholly unavailable, report
the connection failure instead of constructing personalized advice from memory.
Pause the specific task to roll back scheduling; no portfolio mutation or server
deployment is needed to roll back this read-only workflow. Keep task IDs, chat
URLs, private screenshots and account contents out of the product repository.

### Analysis methodology

The reusable [stock analysis protocol](chatgpt-project/04_Stock_Analysis_Protocol.md)
adds the reasoning procedure beyond data access and board formatting. It separates
business/earnings, price-implied expectations and valuation, entry timing, and
portfolio suitability. It requires material counterevidence, attributable inputs,
industry-appropriate valuation and observable invalidation. The accompanying
[integration notes](chatgpt-project/README.md) define synthetic behavioral checks
and their limits. Protocol text does not supply missing fundamental, consensus or
flow feeds, and synthetic compliance does not establish live analytical accuracy.

The methodology source and routing instruction were saved in the ChatGPT project.
Both existing recurring tasks were also updated with a self-contained condensed
method, preserving their schedules. Saved instructions were read back. The
synthetic project exercise checked reasoning boundaries; the updated method has
not yet been verified in a clock-triggered recurring briefing.

