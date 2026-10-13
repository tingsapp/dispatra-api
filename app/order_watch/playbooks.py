"""Conditional recovery suggestions. No resource is claimed available without a feasibility result."""
ACTIONS = {
    'UNASSIGNED': ['Ask the Dispatch agent to evaluate suitable drivers and vehicles.', 'Confirm a revised pickup window if no feasible assignment is available.'],
    'START_OVERDUE': ['Ask the driver for departure status and the reason for the delay.', 'Review replacement options through the Dispatch agent before pickup.'],
    'ARRIVAL_OVERDUE': ['Confirm the driver\'s progress and explain the missed commitment to dispatch.', 'Review the next stops and any receiving deadlines.'],
    'STOP_OVERRUN': ['Ask whether the driver is waiting or loading/unloading, and request an expected finish time.', 'Confirm readiness or access with the pickup/delivery contact.', 'Review later orders on the same route.'],
    'TRAFFIC_RISK': ['Ask the driver to confirm current road conditions when safe.', 'Review an alternative route that satisfies vehicle and access restrictions.', 'Review affected delivery windows with dispatch.'],
    'ARRIVAL_RISK': ['Confirm the driver\'s progress and remaining stops.', 'Review affected time windows and suitable recovery options.'],
    'REPORTED': ['Review the driver\'s report and clarify any missing details.', 'Coordinate suitable assistance or a revised plan; keep cargo custody and customer commitments intact.'],
}


def suggestions(kind, report_kind=None):
    if report_kind == 'UNSAFE': return ['Ask the driver to stop the affected activity safely.', 'Escalate to the dispatcher immediately; arrange appropriate assistance.']
    if report_kind == 'VEHICLE': return ['Confirm the vehicle problem and cargo location.', 'Evaluate a suitable replacement vehicle and a safe cargo handover through dispatch.']
    if report_kind == 'SHORT_LOAD': return ['Confirm the actual quantities collected.', 'Contact the sender and review the dependent deliveries before continuing.']
    if report_kind in {'FAILED_PICKUP','FAILED_DELIVERY','WRONG_ADDRESS'}:
        return ['Confirm the reported access/address/readiness problem.', 'Coordinate correction, another attempt or return with dispatch and the customer.']
    return ACTIONS[kind]


def advice_for_reason(reason):
    return {
        'ITEMS_NOT_READY': ['Confirm the reported readiness estimate with the sender.', 'Review later orders and agree a revised pickup plan if needed.'],
        'WAITING_FOR_STAFF': ['Confirm the expected release time with warehouse staff.', 'Review the effect on later commitments.'],
        'ACCESS_BLOCKED': ['Coordinate gate, building or loading-area access with the contact.', 'Review a revised visit window if access cannot be arranged.'],
        'HANDLING_IN_PROGRESS': ['Compare the reported finish estimate with later commitments.', 'Ask whether additional handling assistance is needed.'],
        'RECIPIENT_UNAVAILABLE': ['Contact the recipient or an authorized receiving contact.', 'Agree another delivery attempt or return plan through dispatch.'],
        'NEED_HELPER': ['Evaluate suitable helpers or handling equipment through dispatch.', 'Review the remaining service window before continuing.'],
        'DAMAGED_ITEM': ['Record the item condition and relevant evidence.', 'Escalate the damage report to dispatch for a documented handling decision.'],
        'VEHICLE_PROBLEM': suggestions('REPORTED','VEHICLE'),
        'UNSAFE': suggestions('REPORTED','UNSAFE'),
        'TRAFFIC': suggestions('TRAFFIC_RISK'),
        'NO_PROBLEM': ['Verify that current milestones are recorded and continue monitoring the booked commitments.'],
    }.get(reason,suggestions('REPORTED'))
