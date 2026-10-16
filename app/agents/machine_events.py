"""Approved metadata signals. These describe completed reads, never provider/customer content."""
WORKSPACE_RECORDS_READ = 'Authorized company records retrieved.'
ORDER_EVIDENCE_REVIEWED = 'Recorded order evidence reviewed.'
RUNTIME_VERIFIED = 'Database connection and restricted runtime role verified.'
READ_SIGNALS = {
    ('CHAT', WORKSPACE_RECORDS_READ): ('data', 'CHAT'),
    ('OPERATIONAL', ORDER_EVIDENCE_REVIEWED): ('data', 'OPERATIONAL'),
    ('TECHNICAL', RUNTIME_VERIFIED): ('data', 'TECHNICAL'),
}
# Exact committed event kinds; unknown/private events are excluded.
EVENT_SIGNALS = {
    'intake.received': ('inputs', 'ORDER', 'Email received', 'INFO'),
    'intake.failed': ('inputs', 'ORDER', 'Email processing failed', 'ERROR'),
    'intake.needs_review': ('ORDER', 'data', 'Email booking needs review', 'WARNING'),
    'intake.unknown_sender': ('ORDER', 'data', 'Email sender needs review', 'WARNING'),
    'mailbox.failed': ('inputs', 'ORDER', 'Mailbox connection failed', 'ERROR'),
    'order.created': ('inputs', 'data', 'Order saved', 'INFO'),
    'order.assigned': ('DISPATCH', 'delivery', 'Driver assigned', 'INFO'),
    'route.created': ('DISPATCH', 'delivery', 'Route prepared', 'INFO'),
    'route.optimized': ('DISPATCH', 'delivery', 'Route optimized', 'INFO'),
    'order.started': ('delivery', 'data', 'Order started', 'INFO'),
    'route.started': ('delivery', 'data', 'Route started', 'INFO'),
    'stop.arrived': ('delivery', 'data', 'Arrival recorded', 'INFO'),
    'stop.completed': ('delivery', 'data', 'Stop completed', 'INFO'),
    'evidence.accepted': ('delivery', 'data', 'Delivery proof accepted', 'INFO'),
    'order.completed': ('delivery', 'data', 'Order completed', 'INFO'),
    'route.completed': ('delivery', 'data', 'Route completed', 'INFO'),
    'order.completion_held': ('delivery', 'data', 'Completion needs review', 'WARNING'),
    'dispatch.no_candidate': ('data', 'DISPATCH', 'Waiting for an eligible driver', 'WARNING'),
    'watch.detected': ('data', 'OPERATIONAL', 'Order risk detected', 'WARNING'),
    'watch.escalated': ('data', 'OPERATIONAL', 'Order risk escalated', 'WARNING'),
    'watch.driver_replied': ('data', 'OPERATIONAL', 'Driver progress reply received', 'INFO'),
    'watch.resolved': ('data', 'OPERATIONAL', 'Order risk resolved', 'INFO'),
}
