"""Structured data parsing class"""

import asyncio
import logging

from elm.ords.llm.calling import BaseLLMCaller, ChatLLMCaller
from elm.ords.utilities import llm_response_as_json
from elm.ords.extraction.tree import AsyncDecisionTree
from permit_toolkit.extraction.decision_trees.graphs import (
    setup_graph_permit_num,
    setup_graph_permit_issue_date,
    setup_graph_permit_expiration_date,
    setup_graph_facility_name,
    setup_graph_facility_address,
    setup_graph_county_name,
    setup_graph_state_name,
    setup_graph_construction_notification,
    setup_graph_construction_notification_window,
    setup_graph_startup_notification,
    setup_graph_startup_notification_window,
    setup_graph_permit_copy_required,
    setup_graph_roe_clause,
    setup_graph_generators,
    # setup_graph_make,
    setup_graph_model,
    setup_graph_fuel,
    setup_graph_tank_size,
    setup_graph_capacity,
    setup_graph_backup,
    setup_graph_control_techs,
    setup_graph_operating_hours,
)

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_MESSAGE = (
    "You are an expert in air quality permits, especially for emergency "
    "standby generators at data centers. Answer strictly based on the "
    "information contained in the provided permit documents and any linked "
    "official references; do not speculate or rely on unsupported "
    "assumptions."
)


def _setup_async_decision_tree(graph_setup_func, **kwargs):
    """Setup Async Decision tree dor ordinance extraction."""
    G = graph_setup_func(**kwargs)  # noqa: N806
    tree = AsyncDecisionTree(G)
    assert len(tree.chat_llm_caller.messages) == 1
    return tree


async def _run_async_tree(tree, response_as_json=True):
    """Run Async Decision Tree and return output as dict."""
    try:
        response = await tree.async_run()
    except RuntimeError:
        msg = (
            "    - NOTE: This is not necessarily an error and may just mean "
            "that the text does not have the requested data."
        )
        logger.exception(msg)
        response = None

    if response_as_json:
        return llm_response_as_json(response) if response else {}

    return response


class StructuredOrdinanceParser(BaseLLMCaller):
    """LLM ordinance document structured data scraping utility."""

    def _init_chat_llm_caller(self, system_message):
        """Initialize a ChatLLMCaller instance for the DecisionTree"""
        return ChatLLMCaller(
            self.llm_service,
            system_message=system_message,
            usage_tracker=self.usage_tracker,
            **self.kwargs,
        )

    async def parse(self, text):
        """Parse text and extract structured ordinance data."""
        refs = await self._run_single_tree(
            setup_func=setup_graph_generators,
            text=text,
            out_key="reference_numbers",
            logger_message="Checking for generators",
        )
        logger.debug("Found the following reference numbers: %s", refs)

        permit_details = {
            "permitNumber": (
                setup_graph_permit_num,
                "permit_number",
                "Checking for permit number",
            ),
            "permitIssuanceDate": (
                setup_graph_permit_issue_date,
                "issue_date",
                "Checking for permit date",
            ),
            "permitExpirationDate": (
                setup_graph_permit_expiration_date,
                "permit_expiration_date",
                "Checking for permit expiration date",
            ),
            "facilityName": (
                setup_graph_facility_name,
                "facility_name",
                "Checking for facility name",
            ),
            "facilityAddress": (
                setup_graph_facility_address,
                "facility_address",
                "Checking for facility address",
            ),
            "facilityCounty": (
                setup_graph_county_name,
                "county_name",
                "Checking for county name",
            ),
            "facilityState": (
                setup_graph_state_name,
                "state_abbr",
                "Checking for state name",
            ),
            "initialConstructionCommencedNotificationRequired": (
                setup_graph_construction_notification,
                "construction_notification_required",
                "Checking if construction notification is required",
            ),
            "constructionCommencedNotificationWindowDays": (
                setup_graph_construction_notification_window,
                "construction_notification_window",
                "Checking for construction notification window",
            ),
            "initialStartupNotificationRequired": (
                setup_graph_startup_notification,
                "startup_notification_required",
                "Checking if startup notification is required",
            ),
            "startupNotificationWindowDays": (
                setup_graph_startup_notification_window,
                "startup_notification_window",
                "Checking for startup notification window",
            ),
            "permitCopyOnsiteRequired": (
                setup_graph_permit_copy_required,
                "permit_copy_required",
                "Checking if a copy of the permit is required onsite",
            ),
            "rightOfEntryClause": (
                setup_graph_roe_clause,
                "roe_clause",
                "Checking for right of entry clause",
            ),
        }
        tasks = {
            name: asyncio.create_task(
                self._run_single_tree(
                    setup_func=f,
                    text=text,
                    out_key=k,
                    logger_message=m,
                )
            )
            for name, (f, k, m) in permit_details.items()
        }
        logger.debug(
            "Starting permit info extraction with %d tasks.", len(tasks)
        )

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        permit_details = {}
        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("Task %s failed: %s", key, result)
                permit_details[key] = None
            else:
                permit_details[key] = result

        values = {"permitDetails": permit_details}
        logger.debug("Permit info extraction complete.")

        generator_details = {
            "make": (
                setup_graph_generators,
                "make",
                "Checking for generator make",
            ),
            "model": (
                setup_graph_model,
                "model",
                "Checking for generator model",
            ),
            "fuel_type": (
                setup_graph_fuel,
                "fuel_type",
                "Checking for generator fuel type",
            ),
            "tank_size": (
                setup_graph_tank_size,
                "tank_size",
                "Checking for generator tank size",
            ),
            # "capacity": self._check_capacity,
            "backup_mw": (
                setup_graph_backup,
                "backup_mw",
                "Checking for generator backup",
            ),
            "control_technologies": (
                setup_graph_control_techs,
                "control_technologies",
                "Checking for generator control techs",
            ),
            "operating_hours_limit_yr": (
                setup_graph_operating_hours,
                "operating_hours",
                "Checking for generator operating hours",
            ),
            # "emissions_limits": (
            #     setup_graph_emissions,
            #     "emissions_limits",
            #     "Checking for generator emissions limits",
            # ),
        }
        tasks = {
            (ref_number, name): asyncio.create_task(
                self._run_single_tree(
                    setup_func=f,
                    text=text,
                    out_key=k,
                    logger_message=m,
                    ref_number=ref_number,
                )
            )
            for ref_number in refs
            for name, (f, k, m) in generator_details.items()
        }
        logger.debug("Starting value extraction with %d tasks.", len(tasks))

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        generators = {
            ref_number: {"referenceNumber": ref_number} for ref_number in refs
        }
        for (ref_number, key), result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("Task %s failed: %s", key, result)
                generators[ref_number][key] = None
            else:
                generators[ref_number][key] = result

        values["generatorSets"] = [
            generators[ref_number] for ref_number in refs
        ]

        logger.debug("Value extraction complete.")

        return values

    async def _run_single_tree(
        self, setup_func, text, out_key, logger_message, **extra_kwargs
    ):
        if "ref_number" in extra_kwargs:
            logger_message = (
                f"{logger_message} for ref number {extra_kwargs['ref_number']}"
            )

        logger.debug(logger_message)
        tree = _setup_async_decision_tree(
            setup_func,
            text=text,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
            **extra_kwargs,
        )
        dtree_out = await _run_async_tree(tree)
        return dtree_out.get(out_key, None)

    async def _check_capacity(self, text, ref_number):
        logger.debug(
            "Checking for generator capacity for ref number %s", ref_number
        )
        tree = _setup_async_decision_tree(
            setup_graph_capacity,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_capacity_out = await _run_async_tree(tree)
        return {
            "rated_capacity_kw": dtree_capacity_out.get("capacity_kw", None),
            "rated_capacity_hp": dtree_capacity_out.get("capacity_hp", None),
        }
