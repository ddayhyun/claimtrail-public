"""계산 함수 세 개.

이 파일은 **일부러** ruff format 기준에 맞지 않게 적혀 있다(작은따옴표, 불필요한
줄바꿈). `ruff check` 는 통과하고 `ruff format --check` 만 실패하도록 -- 샘플 리포트에서
'테스트·lint 통과, 형식 검사 실패' 를 보여 주기 위해서다. 고치려면 `ruff format .` 을
실행하면 되고, 그러면 Claimtrail 판정은 통과(종료 코드 0)로 바뀐다.
"""

from __future__ import annotations


def add(a: float, b: float) -> float:
    '''두 수를 더한다.'''
    return a + b


def divide(a: float, b: float) -> float:
    '''a 를 b 로 나눈다. 0 으로 나누면 ValueError.'''
    if b == 0:
        raise ValueError(
            '0 으로 나눌 수 없다'
        )
    return a / b


def mean(values: list[float]) -> float:
    '''평균. 빈 목록이면 ValueError.'''
    if not values:
        raise ValueError('빈 목록의 평균은 정의되지 않는다')
    return sum(values) / len(values)
