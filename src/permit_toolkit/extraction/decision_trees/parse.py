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
    setup_graph_num_gens,
    setup_graph_included_in_permit,
    setup_graph_make,
    setup_graph_model,
    setup_graph_rated_capacity_kw,
    setup_graph_rated_capacity_hp,
    setup_graph_rated_capacity_bhp,
    setup_graph_max_capacity_kw,
    setup_graph_max_capacity_hp,
    setup_graph_max_capacity_bhp,
    setup_graph_fuel,
    setup_graph_secondary_fuel,
    setup_graph_other_fuel,
    setup_graph_fuel_grade,
    setup_graph_fuel_spec,
    setup_graph_fuel_sulphur,
    # setup_graph_fuel_cert_required,
    # setup_graph_fuel_cert_fields,
    # setup_graph_fuel_change_trigger,
    setup_graph_fuel_throughput_limit,
    setup_graph_control_techs,
    setup_graph_operating_hours,
    setup_graph_operating_window,
    setup_graph_operating_modes,
    # setup_graph_opacity,
    # setup_graph_hour_meter,
    # setup_graph_record_years,
    # setup_graph_operation_reason_log,
    # setup_graph_manufacturers_o_and_m,
    # setup_graph_maintenance_records,
    # setup_graph_nsps,
    # setup_graph_mact,
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

    async def parse(self, text, extract_sem):
        """Parse text and extract structured ordinance data."""
        ref_out = await self._run_single_tree(
            setup_func=setup_graph_generators,
            text=text,
            logger_message="Checking for generators",
            extract_sem=extract_sem,
        )
        refs = ref_out.get("reference_numbers", [])
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
            (name, k): asyncio.create_task(
                self._run_single_tree(
                    setup_func=f,
                    text=text,
                    logger_message=m,
                    extract_sem=extract_sem,
                ),
                name=name,
            )
            for name, (f, k, m) in permit_details.items()
        }
        logger.debug(
            "Starting permit info extraction with %d tasks.", len(tasks)
        )

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        permit_details = {}
        for (key, dtk), result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("Task %s failed: %s", key, result)
                permit_details[key] = None
            else:
                permit_details[key] = result.get(dtk)

        values = {"permitDetails": permit_details}
        logger.debug("Permit info extraction complete.")

        if not refs:
            values["generatorSets"] = []
            logger.debug("No generators found; skipping generator extraction.")
            return values

        generator_details = {
            "numGenerators": (
                setup_graph_num_gens,
                "generator_count",
                "Checking for number of generators",
            ),
            "includedInPermitProject": (
                setup_graph_included_in_permit,
                "included_in_permit_project",
                "Checking if generator is included in permit project",
            ),
            "make": (
                setup_graph_make,
                "make",
                "Checking for generator make",
            ),
            "model": (
                setup_graph_model,
                "model",
                "Checking for generator model",
            ),
            "ratedCapacityKW": (
                setup_graph_rated_capacity_kw,
                "rated_capacity_kw",
                "Checking for generator rated capacity in kW",
            ),
            "ratedCapacityHP": (
                setup_graph_rated_capacity_hp,
                "rated_capacity_hp",
                "Checking for generator rated capacity in HP",
            ),
            "ratedCapacityBHP": (
                setup_graph_rated_capacity_bhp,
                "rated_capacity_bhp",
                "Checking for generator rated capacity in BHP",
            ),
            "maxCapacityKW": (
                setup_graph_max_capacity_kw,
                "max_capacity_kw",
                "Checking for generator maximum capacity in kW",
            ),
            "maxCapacityHP": (
                setup_graph_max_capacity_hp,
                "max_capacity_hp",
                "Checking for generator maximum capacity in HP",
            ),
            "maxCapacityBHP": (
                setup_graph_max_capacity_bhp,
                "max_capacity_bhp",
                "Checking for generator maximum capacity in BHP",
            ),
            "primaryFuelType": (
                setup_graph_fuel,
                "fuel_type",
                "Checking for generator fuel type",
            ),
            "secondaryFuelType": (
                setup_graph_secondary_fuel,
                "secondary_fuel_type",
                "Checking for generator secondary fuel type",
            ),
            "otherFuels": (
                setup_graph_other_fuel,
                "other_fuel_types",
                "Checking for generator other fuel type",
            ),
            "fuelGrade": (
                setup_graph_fuel_grade,
                "fuel_grade",
                "Checking for generator fuel grade",
            ),
            "fuelSpecification": (
                setup_graph_fuel_spec,
                "fuel_spec",
                "Checking for generator fuel specification",
            ),
            "fuelSulfurContentPct": (
                setup_graph_fuel_sulphur,
                "fuel_sulfur_pct",
                "Checking for generator fuel sulphur content",
            ),
            # "fuelCertificationRequired": (
            #     setup_graph_fuel_cert_required,
            #     "fuel_cert_required",
            #     "Checking if fuel supplier certification is required",
            # ),
            # "fuelCertificationFields": (
            #     setup_graph_fuel_cert_fields,
            #     "fuel_cert_fields",
            #     "Checking for required fuel supplier certification fields",
            # ),
            # "fuelChangePermitTrigger": (
            #     setup_graph_fuel_change_trigger,
            #     "fuel_change_trigger",
            #     "Checking for fuel change trigger",
            # ),
            "fuelThroughputLimit": (
                setup_graph_fuel_throughput_limit,
                "fuel_limit",
                "Checking for fuel throughput limit",
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
            "operatingHoursRollingWindow": (
                setup_graph_operating_window,
                "operating_window",
                "Checking for generator operating window",
            ),
            "allowedOperatingModes": (
                setup_graph_operating_modes,
                "operating_modes",
                "Checking for generator operating modes",
            ),
            # "opacityLimitPercent": (
            #     setup_graph_opacity,
            #     "opacity_limit_pct",
            #     "Checking for generator opacity limits",
            # ),
            # "hourMeterRequired": (
            #     setup_graph_hour_meter,
            #     "hour_meter_device_required",
            #     "Checking if hour metering device is required",
            # ),
            # "recordkeepingWindowYears": (
            #     setup_graph_record_years,
            #     "min_record_years",
            #     "Checking for hour meter record keeping years",
            # ),
            # "operationReasonLogRequired": (
            #     setup_graph_operation_reason_log,
            #     "operation_reason_log_required",
            #     "Checking for operating reasons logging requirements",
            # ),
            # "manufacturerOandMRequired": (
            #     setup_graph_manufacturers_o_and_m,
            #     "manufacturers_instructions_required",
            #     "Checking for manufacturer's operation and maintenance "
            #     "instructions requirements",
            # ),
            # "maintenanceTrainingRecordsRequired": (
            #     setup_graph_maintenance_records,
            #     "maintenance_records_required",
            #     "Checking for maintenance and operator training records "
            #     "requirements",
            # ),
            # "nspsSubpartIIII": (
            #     setup_graph_nsps,
            #     "nsps_applicable",
            #     "Checking for NSPS Subpart IIII applicability",
            # ),
            # "mactSubpartZZZZ": (
            #     setup_graph_mact,
            #     "mact_applicable",
            #     "Checking for MACT Subpart ZZZZ applicability",
            # ),
        }
        tasks = {
            (ref_number, name, k): asyncio.create_task(
                self._run_single_tree(
                    setup_func=f,
                    text=text,
                    logger_message=m,
                    ref_number=ref_number,
                    extract_sem=extract_sem,
                ),
                name=f"{ref_number}: {name}",
            )
            for ref_number in refs
            for name, (f, k, m) in generator_details.items()
        }
        logger.debug("Starting value extraction with %d tasks.", len(tasks))

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        generators = {
            ref_number: {"referenceNumber": ref_number} for ref_number in refs
        }
        for (ref_number, key, dtk), result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("Task %s failed: %s", key, result)
                generators[ref_number][key] = None
                if key == "fuelThroughputLimit":
                    generators[ref_number]["fuelThroughputScope"] = None
                    generators[ref_number]["fuelThroughputGroupRef"] = None
                elif key == "hourMeterRequired":
                    generators[ref_number]["observationFrequency"] = None
            else:
                generators[ref_number][key] = result.get(dtk)
                if key == "fuelThroughputLimit":
                    generators[ref_number]["fuelThroughputScope"] = result.get(
                        "scope"
                    )
                    generators[ref_number]["fuelThroughputGroupRef"] = (
                        result.get("group")
                    )
                elif key == "hourMeterRequired":
                    generators[ref_number]["observationFrequency"] = (
                        result.get("obs_freq")
                    )

        values["generatorSets"] = [
            generators[ref_number] for ref_number in refs
        ]

        logger.debug("Value extraction complete.")

        return values

    async def _run_single_tree(
        self, setup_func, text, logger_message, extract_sem, **extra_kwargs
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

        async with extract_sem:
            return await _run_async_tree(tree)
