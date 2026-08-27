import asyncio

import pytest

from app.linkedin_client import LinkedInDailyLimitExceeded, _DailyRequestBudget


def test_daily_budget_allows_up_to_limit_then_raises():
    async def scenario():
        budget = _DailyRequestBudget(limit=3)
        await budget.consume()
        await budget.consume()
        await budget.consume()
        with pytest.raises(LinkedInDailyLimitExceeded):
            await budget.consume()

    asyncio.run(scenario())


def test_daily_budget_zero_limit_disables_cap():
    async def scenario():
        budget = _DailyRequestBudget(limit=0)
        for _ in range(50):
            await budget.consume()  # should never raise

    asyncio.run(scenario())
