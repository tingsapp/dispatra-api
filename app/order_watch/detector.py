"""Pure, contextual signals from authoritative milestones and qualified provider estimates."""
from datetime import timedelta
from app.dispatch.explanations import no_candidate_summary, exclusion_lines
from .evidence import assignment_deadline,deadline,fact,instant
from .playbooks import suggestions


def signal(key,kind,title,description,evidence,stop_id=None,severity='WARNING',certainty='OBSERVED',due=None):
    return dict(fingerprint=key,kind=kind,title=title,description=description,evidence=evidence,stop_id=stop_id,
        source_issue_id=None,severity=severity,certainty=certainty,suggestions=suggestions(kind),response_due_at=due)


def evaluate(order,route,visits,stops,estimate,current,no_candidate=None):
    result=[]
    if order.status in {'COMPLETED','CANCELLED'}:return result
    if order.route_id is None:
        due=assignment_deadline(s for s in stops.values() if s.order_id==order.id)
        overdue=due is not None and due<=current
        # The Dispatch agent already exposes a recorded NO_DRIVER alert (with
        # every exclusion reason).  A failed candidate search by itself is not
        # a second operational incident; creating one made the Monitor show
        # the same unassigned order twice.  Operational monitoring adds value
        # here only once the booked assignment deadline has actually passed.
        if overdue:
            evidence=[fact('Assignment','Unassigned','Order record',current)]
            if due: evidence.insert(0,fact('Booked arrival deadline',due.isoformat(),'Booked stop window'))
            description = 'Deadline passed with no driver.'
            if no_candidate:
                excluded = no_candidate.excluded
                evidence.append(fact('Dispatch result','No candidate in the recorded dispatch screening; road feasibility requires validation','Dispatch agent decision',no_candidate.created_at))
                for line in exclusion_lines(excluded):
                    evidence.append(fact('Driver eligibility check',line,'Dispatch agent decision',no_candidate.created_at))
                # Lists show one line; every driver check remains in the evidence.
                description += ' ' + no_candidate_summary(excluded)
            result.append(signal(f'unassigned:{order.id}:{order.version}','UNASSIGNED','Order needs an assignment',
                description,
                evidence,severity='CRITICAL' if overdue else 'WARNING',due=due))
        return result
    if not route:return result
    mine=[v for v in visits if stops[v.stop_id].order_id==order.id and v.status!='COMPLETED']
    if route.status=='PLANNED' and current>route.planned_at and mine:
        result.append(signal(f'start:{order.id}:{route.id}:{route.generation}','START_OVERDUE','Route start needs confirmation',
            'Planned start passed; route not started.',
            [fact('Planned route start',route.planned_at.isoformat(),'Published route plan'),fact('Route state','Planned; start not recorded','Route record',current)],
            certainty='NEEDS_CONFIRMATION',due=min([deadline(order,stops[v.stop_id]) for v in mine if deadline(order,stops[v.stop_id])] or [route.planned_at])))
    next_visit=next((v for v in visits if v.status!='COMPLETED'),None)
    for visit in mine:
        stop=stops[visit.stop_id];due=deadline(order,stop)
        if visit.arrived_at is None and due and current>due:
            result.append(signal(f'arrival:{visit.id}:{route.generation}','ARRIVAL_OVERDUE','Arrival deadline passed',
                'Arrival deadline passed; arrival not recorded.',
                [fact('Arrival deadline',due.isoformat(),'Booked stop window'),fact('Arrival','Not recorded','Stop execution',current)],
                stop.id,'CRITICAL','NEEDS_CONFIRMATION',due))
        if visit.arrived_at is not None and next_visit and visit.id==next_visit.id:
            planned_minutes=stop.data.get('service_minutes')
            first=next((s for s in order.facts['stops'] if s['kind']=='PICKUP'),None)
            ready_at=instant(stop.data.get('window_start')) or (order.scheduled_at if first and first['id']==str(stop.id) else visit.arrived_at)
            clock_start=max(visit.arrived_at,ready_at)
            elapsed=max(0,int((current-clock_start).total_seconds()/60))
            # Exceeding the recorded plan triggers clarification, never an invented waiting/loading cause.
            if planned_minutes and elapsed>planned_minutes:
                result.append(signal(f'stop:{visit.id}:{route.generation}','STOP_OVERRUN','Stop progress needs clarification',
                    f'At stop {elapsed} min; plan allows {planned_minutes} min.',
                    [fact('Recorded arrival',visit.arrived_at.isoformat(),'Stop execution',visit.arrived_at),fact('Elapsed service-eligible time at stop',f'{elapsed} minutes','Recorded arrival and booked service window',current),
                     fact('Planned stop service',f'{planned_minutes} minutes','Booked stop plan')],stop.id,certainty='NEEDS_CONFIRMATION',due=due))
        if visit.arrived_at is not None or not estimate:continue
        prediction=next((e for e in estimate.data['stops'] if e['stop_id']==str(stop.id)),None)
        if prediction and due:
            eta=instant(prediction['eta'])
            if eta>due:
                traffic=prediction['traffic_added_seconds']
                kind='TRAFFIC_RISK' if traffic>0 else 'ARRIVAL_RISK'
                result.append(signal(f'prediction:{visit.id}:{route.generation}',kind,'Arrival window at risk',
                    f'Expected about {max(1,round((eta-due).total_seconds()/60))} min late.',
                    [fact('Predicted arrival',eta.isoformat(),estimate.data['source'],estimate.observed_at),
                     fact('Arrival deadline',due.isoformat(),'Booked stop window'),
                     fact('Extra travel against provider baseline',f'{round(traffic/60,1)} minutes','Traffic delay reported or derived from provider baseline',estimate.observed_at),
                     fact('Remaining road distance',f'{round(prediction["road_distance_metres"]/1000,1)} km','Routing provider',estimate.observed_at),
                     fact('Position captured',estimate.captured_at.isoformat(),'Driver location',estimate.captured_at),
                     fact('Distance from next stop',f"{estimate.data.get('straight_line_metres')} metres straight-line; reported accuracy {estimate.data.get('accuracy_metres')} metres. Proximity does not confirm arrival.",'Qualified location calculation',estimate.captured_at)],stop.id,certainty='PREDICTED',due=due))
    return result
