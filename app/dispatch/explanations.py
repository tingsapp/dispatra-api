"""Present recorded hard-check results without inventing additional blockers."""


def exclusion_lines(excluded):
    result = []
    for item in excluded:
        name = item.get('driver_name') or 'Driver'
        number = item.get('driver_number')
        label = f'{name} ({number})' if number else name
        result.append(f'{label}: {item["reason"]}')
    return result


def no_candidate_description(excluded):
    lines = exclusion_lines(excluded)
    return 'Recorded dispatch check found no eligible driver:\n' + '\n'.join(lines) if lines else 'The recorded dispatch check found no active driver.'
