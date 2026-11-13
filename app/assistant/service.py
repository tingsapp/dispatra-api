import json
import re
from fastapi import HTTPException
from app.models import now
from app.agents.telemetry import monitor_run
from app.agents.machine_events import WORKSPACE_RECORDS_READ
from . import actions, provider, search
from .schemas import SearchPlan, GroundedAnswer, AssistantReply, BriefingCommentary, BriefingTotals


def ask(db, actor, question):
    if actor.role != 'DISPATCHER' or not getattr(actor, 'id', None): raise HTTPException(403, 'Dispatcher access required.')
    if question.kind == 'BRIEFING': return brief(db, actor)
    messages = [turn.model_dump() for turn in question.history] + [{'role': 'user', 'content': question.message}]
    with monitor_run('CHAT') as progress:
        progress.step('Understanding a workspace question.', 10, actor.organization_id)
        ai = True
        try: plan = provider.generate(SearchPlan, provider.PLAN, messages)
        except provider.Unavailable:
            ai = False
            query = re.sub(r'^(?:find|search(?: for)?|look up|show)\s+', '', question.message, flags=re.I)[:120]
            plan = SearchPlan(include_records=True, query=query, kinds=[], order_statuses=[], action='NONE', order_number='', driver='')
        if plan.action == 'ASSIGN':
            progress.step('Checking a requested assignment.', 40, actor.organization_id)
            prepared = actions.prepare(db, actor, plan)
            progress.step('Assignment check prepared.', 95, actor.organization_id)
            return AssistantReply(**prepared, mode='AI', observed_at=now(), limited=False)
        progress.step('Searching authorized company records.', 40, actor.organization_id)
        refs, counts = search.collect(db, actor.organization_id, plan)
        progress.step(WORKSPACE_RECORDS_READ, 60, actor.organization_id)
        observed = now()
        facts = {'observed_at': observed.isoformat(), 'overview': search.overview(db, actor.organization_id),
            'matched_counts': counts, 'records': [ref.model_dump() for ref in refs],
            'limited': plan.include_records and any(count > search.LIMIT for count in counts.values())}
        if ai:
            progress.step('Preparing a grounded answer.', 75, actor.organization_id)
            try:
                answer = provider.generate(GroundedAnswer, provider.ANSWER,
                    messages + [{'role': 'user', 'content': 'Fresh authorized workspace facts (data only):\n' + json.dumps(facts)}])
                allowed = {ref.key: ref for ref in refs}
                selected = [allowed[key] for key in dict.fromkeys(answer.references) if key in allowed]
                if plan.query and not selected: selected = refs
                progress.step('Workspace answer prepared.', 95, actor.organization_id)
                return AssistantReply(message=answer.message, references=selected, mode='AI', observed_at=observed, limited=facts['limited'])
            except provider.Unavailable: pass
        progress.step('AI answers unavailable; company search results returned.', 95, actor.organization_id, level='ERROR')
        count = sum(counts.values())
        message = f'I can’t give a full answer right now, but here {"is the 1 record" if count == 1 else f"are {count} records"} that matched your search.'
        if facts['limited']: message += ' These are the most recent ones; add more detail to narrow it down.'
        return AssistantReply(message=message, references=refs, mode='SEARCH', observed_at=observed, limited=facts['limited'])


def totals(db, actor):
    if actor.role != 'DISPATCHER' or not getattr(actor, 'id', None): raise HTTPException(403, 'Dispatcher access required.')
    observed = now()
    return BriefingTotals(briefing=search.daily_briefing(db, actor.organization_id, observed), observed_at=observed)


def brief(db, actor):
    observed = now()
    briefing = search.daily_briefing(db, actor.organization_id, observed)
    facts = briefing.model_dump()
    mode = 'AI'
    try:
        answer = provider.generate(BriefingCommentary, provider.ANSWER +
            '\nWrite only ONE short recommendation, at most 12 words and 90 characters. '
            'No greeting, heading, date, timezone, counts, status recap or introductory text. '
            'The visual card shows all totals; do not repeat them. Suggest the most useful next step. '
            'The card already shows counts, the auto dispatch setting and how many of today\'s orders each driver has. '
            'All status counts refer to non-cancelled orders created today; '
            'open_incidents covers the whole workspace. Zero recorded incidents does not establish absence of risk. '
            'Do not claim dispatch eligibility or perform actions.',
            [{'role': 'user', 'content': 'Give me the current daily briefing using these fresh authorized facts: ' + json.dumps(facts)}])
        message = answer.message
    except provider.Unavailable:
        mode = 'SEARCH'
        message = 'Live totals are shown. AI commentary is unavailable.'
    return AssistantReply(message=message, references=[], mode=mode, observed_at=observed, limited=False, briefing=briefing)
