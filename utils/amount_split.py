"""
금액 분할 유틸리티 - 10원 단위 나머지 배분법

총액을 n명에게 나눌 때 나누어떨어지지 않는 금액을 10원 단위로 공정하게 배분합니다.
예: 333,330원 ÷ 4명 → [83,340, 83,330, 83,330, 83,330] (합계 333,330원)

* 나머지가 0이면(딱 떨어지면) 전원 동일 금액
* 나머지가 있을 때만 10원 단위 배분 적용
"""
from decimal import Decimal


def split_amount_10won(
    total_amount: Decimal,
    count: int,
    unit: int = 10,
    extra_recipient_indices: list[int] | None = None,
) -> list[Decimal]:
    """
    10원 단위 나머지 배분법으로 금액을 count명에게 분할합니다.

    Args:
        total_amount: 총 금액
        count: 분할 대상 인원 수
        unit: 금액 단위 (기본 10원)
        extra_recipient_indices: +unit원을 부담할 참가자들의 인덱스 (0-based).
            미지정 시 앞에서부터 순서대로 배분.
            예: [2] → 3번째 참가자가 나머지 10원 부담

    Returns:
        각 인원별 부담 금액 리스트 (합계 = total_amount)
    """
    if count <= 0:
        return []

    total = int(total_amount)
    base = (total // count // unit) * unit
    remainder = total - base * count

    # 나머지가 0이면 딱 떨어짐 → 전원 동일 금액 (10원 배분 적용 안 함)
    if remainder == 0:
        each = Decimal(str(total // count))
        return [each] * count

    extra_count = remainder // unit
    if extra_count == 0:
        each = Decimal(str(total // count))
        return [each] * count

    if extra_recipient_indices is not None and extra_count > 0:
        preferred = [i for i in extra_recipient_indices if 0 <= i < count][:extra_count]
        extra_indices_set = set(preferred)
        # 사용자가 지정한 인원이 부족하면 나머지는 앞에서부터 채움
        for i in range(count):
            if len(extra_indices_set) >= extra_count:
                break
            if i not in extra_indices_set:
                extra_indices_set.add(i)
    else:
        extra_indices_set = set(range(extra_count))

    amounts: list[Decimal] = []
    for i in range(count):
        amt = base + (unit if i in extra_indices_set else 0)
        amounts.append(Decimal(str(amt)))
    return amounts


def allocate_by_total(
    item_amount: Decimal,
    total_cost: Decimal,
    total_per_person: list[Decimal],
) -> list[Decimal]:
    """
    총액 기준 1인당 금액에 따라 항목 금액을 비례 배분합니다.
    총액이 나누어떨어질 때 전원 동일 부담금을 보장합니다.

    Args:
        item_amount: 항목 금액 (예: 그린피 20000)
        total_cost: 총 비용 (예: 90000)
        total_per_person: split_amount_10won(total_cost, n) 결과

    Returns:
        각 참가자별 해당 항목 부담금
    """
    if not total_per_person or total_cost <= 0:
        return []
    n = len(total_per_person)
    if n == 0:
        return []
    result: list[Decimal] = []
    quantize = Decimal('0.01')
    for j in range(n - 1):
        share = (item_amount * total_per_person[j] / total_cost).quantize(quantize)
        result.append(share)
    last = item_amount - sum(result)
    result.append(last)
    return result


def allocate_items_to_exact_totals(
    total_per_person: list[Decimal],
    item_amounts: list[Decimal],
    total_cost: Decimal,
) -> list[list[Decimal]]:
    """
    항목별 금액을 배분하되, 각 참가자의 합계가 total_per_person과 정확히 일치하도록 함.
    10원 단위 total_per_person이 그대로 반영됩니다.

    Args:
        total_per_person: split_amount_10won(total_cost, n) 결과 (10원 단위)
        item_amounts: 각 항목 금액 리스트 (그린피, 캐디피, 카트비, 기타...)
        total_cost: 총 비용

    Returns:
        result[i][j] = 항목 i의 참가자 j 부담액, sum_i result[i][j] == total_per_person[j]
    """
    if not total_per_person or not item_amounts or total_cost <= 0:
        return []
    n = len(total_per_person)
    result: list[list[Decimal]] = []
    accumulated = [Decimal('0')] * n
    for i, amt in enumerate(item_amounts[:-1]):
        shares = allocate_by_total(amt, total_cost, total_per_person)
        result.append(shares)
        for j in range(n):
            accumulated[j] += shares[j]
    last_shares = [total_per_person[j] - accumulated[j] for j in range(n)]
    result.append(last_shares)
    return result
