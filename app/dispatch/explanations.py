"""Present recorded hard-check results without inventing additional blockers."""


def exclusion_lines(excluded):
    result = []
    for item in excluded:
        name = item.get('driver_name') or 'Driver'
        number = item.get('driver_number')
        label = f'{name} ({number})' if number else name
        result.append(f'{label}: {item["reason"]}')
    return result


def no_candidate_summary(excluded):
    """One short line for lists; per-driver reasons stay on the dispatch decision and incident evidence."""
    return f'No eligible driver ({len(excluded)} checked).' if excluded else 'No active drivers.'
