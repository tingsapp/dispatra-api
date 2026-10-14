import json
import re
from fastapi import HTTPException
from app.models import now
from app.agents.telemetry import monitor_run
from . import provider, search
from .schemas import SearchPlan, GroundedAnswer, AssistantReply


def ask(db, actor, question):
    if actor.role != 'DISPATCHER' or not getattr(actor, 'id', None): raise HTTPException(403, 'Dispatcher access required.')
    messages = [turn.model_dump() for turn in question.history] + [{'role': 'user', 'content': question.message}]
    with monitor_run('CHAT') as progress:
        progress.step('Understanding a workspace question.', 10, actor.organization_id)
        ai = True
        try: plan = provider.generate(SearchPlan, provider.PLAN, messages)
        except provider.Unavailable:
            ai = False
            query = re.sub(r'^(?:find|search(?: for)?|look up|show)\s+', '', question.message, flags=re.I)[:120]
            plan = SearchPlan(include_records=True, query=query, kinds=[], order_statuses=[])
        progress.step('Searching authorized company records.', 40, actor.organization_id)
        refs, counts = search.collect(db, actor.organization_id, plan)
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
        message = f'AI answers are unavailable right now. Search found {count} matching record{"s" if count != 1 else ""}.'
        if facts['limited']: message += ' Showing the most recent matches; refine your search for more.'
        return AssistantReply(message=message, references=refs, mode='SEARCH', observed_at=observed, limited=facts['limited'])
