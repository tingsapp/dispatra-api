"""Chat replies and change cards shared by every kind of chat change."""
from . import records
from .actions import order_reference, driver_reference
from .schemas import AssistantChange, AssistantReference


def reply(message, references=(), change=None):
    return {'message': message, 'references': list(references), 'action': None, 'change': change}


def ask(problems, noun):
    if len(problems) == 1: return reply(problems[0])
    return reply(f'I need a bit more before I can prepare this {noun}:\n' + '\n'.join(f'- {item}' for item in problems))


def proposal(name, op, subject, body, lines, target_id=None, references=()):
    entity, _, _ = name.partition('.')
    change = AssistantChange(operation=name, title=op.title or f'{op.verb} {records.NOUNS[entity]}', subject=subject, target_id=target_id,
        destructive=op.destructive, lines=lines, body=body)
    return reply(f'Here’s the change for {subject}. Nothing is saved until you confirm below.', references, change)


def reference(entity, row):
    if entity == 'ORDER': return [order_reference(row)]
    if entity == 'DRIVER': return [driver_reference(row)]
    if entity in {'SHIPPER', 'VEHICLE'}:
        return [AssistantReference(key=f'{entity}:{row.id}', kind=entity, id=str(row.id), label=records.label(entity, row), detail=row.number)]
    return []
