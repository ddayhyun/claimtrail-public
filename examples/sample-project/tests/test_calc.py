"""샘플 테스트. 전부 통과한다 -- 리포트의 'pytest 통과' 줄을 만든다."""

import pytest

from sample_calc import add, divide, mean


def test_add():
    assert add(2, 3) == 5


def test_divide():
    assert divide(10, 4) == 2.5


def test_divide_by_zero_raises():
    with pytest.raises(ValueError):
        divide(1, 0)


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_mean_empty_raises():
    with pytest.raises(ValueError):
        mean([])
