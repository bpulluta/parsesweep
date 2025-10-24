# -*- coding: utf-8 -*-
"""ELM Ordinance structured parsing class."""
import asyncio
import logging

from elm.ords.llm.calling import BaseLLMCaller, ChatLLMCaller
from elm.ords.utilities import llm_response_as_json
from elm.ords.extraction.tree import AsyncDecisionTree
from extraction.graphs import (
    setup_graph_permit_num,
    setup_graph_generators,
    setup_graph_make,
    setup_graph_model,
    setup_graph_fuel,
    setup_graph_tank_size,
    setup_graph_capacity,
    setup_graph_backup,
    setup_graph_control_techs,
    setup_graph_operating_hours,
    setup_graph_emissions
)

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_MESSAGE = (
    "You are a legal expert extracting data from emergency generator "
    "permits for data centers."
)

def _setup_async_decision_tree(graph_setup_func, **kwargs):
    """Setup Async Decision tree dor ordinance extraction."""
    G = graph_setup_func(**kwargs)
    tree = AsyncDecisionTree(G)
    assert len(tree.chat_llm_caller.messages) == 1
    return tree

async def _run_async_tree(tree, response_as_json=True):
    """Run Async Decision Tree and return output as dict."""
    try:
        response = await tree.async_run()
    except RuntimeError:
        logger.error(
            "    - NOTE: This is not necessarily an error and may just mean "
            "that the text does not have the requested data."
        )
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
        permit_num = await self._get_permit_num(text)
        refs = await self._get_generator_refs(text)
        values = {'permitNumber': permit_num}
        generators = {}
        for ref_number in refs:
            gen_values = {}
            check_map = {
                "make": self._check_make,
                "model": self._check_model,
                "fuel_type": self._check_fuel_type,
                "tank_size": self._check_tank_size,
                "capacity": self._check_capacity,
                "backup_mw": self._check_backup,
                "control_technologies": self._check_techs,
                "operating_hours": self._check_op_hours,
                "emissions_limits": self._check_emissions,
            }

            tasks = {name: asyncio.create_task(func(text, ref_number)) for name, func in check_map.items()}

            logger.debug("Starting value extraction with %d tasks.", len(tasks))

            results = await asyncio.gather(*tasks.values(), return_exceptions=True)

            for key, result in zip(tasks.keys(), results):
                if isinstance(result, Exception):
                    logger.warning("Task %s failed: %s", key, result)
                    gen_values[key] = None
                else:
                    gen_values[key] = result
            generators[ref_number] = gen_values

        values['generators'] = generators

        breakpoint()
        
        logger.debug("Value extraction complete.")

        return values
    
    async def _get_permit_num(self, text):
        logger.debug("Checking for permit number")
        tree = _setup_async_decision_tree(
            setup_graph_permit_num,
            text=text,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_permit_out = await _run_async_tree(tree)

        permit_num = dtree_permit_out.get("permit_number", None)

        return permit_num

    async def _get_generator_refs(self, text):
        logger.debug("Checking for generators")
        tree = _setup_async_decision_tree(
            setup_graph_generators,
            text=text,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_refs_out = await _run_async_tree(tree)

        gen_refs = dtree_refs_out.get("referenceNumbers", [])

        return gen_refs
    
    async def _check_make(self, text, ref_number):
        logger.debug("Checking for generator make for ref number %s", ref_number)
        tree = _setup_async_decision_tree(
            setup_graph_make,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_make_out = await _run_async_tree(tree)

        make = dtree_make_out.get("make", None)

        return make

    async def _check_model(self, text, ref_number):
        logger.debug("Checking for generator model for ref number %s", ref_number)
        tree = _setup_async_decision_tree(
            setup_graph_model,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_model_out = await _run_async_tree(tree)

        model = dtree_model_out.get("model", None)

        return model

    async def _check_fuel_type(self, text, ref_number):
        logger.debug("Checking for generator fuel type for ref number %s", ref_number)
        tree = _setup_async_decision_tree(
            setup_graph_fuel,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_fuel_type_out = await _run_async_tree(tree)

        fuel_type = dtree_fuel_type_out.get("fuel_type", None)

        return fuel_type
    
    async def _check_tank_size(self, text, ref_number):
        logger.debug("Checking for generator tank size for ref number %s", ref_number)
        tree = _setup_async_decision_tree(
            setup_graph_tank_size,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_tank_out = await _run_async_tree(tree)

        tank_size = dtree_tank_out.get("tank_size", None)

        return dtree_tank_out
    
    async def _check_capacity(self, text, ref_number):
        logger.debug("Checking for generator capacity for ref number %s", ref_number)
        tree = _setup_async_decision_tree(
            setup_graph_capacity,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_capacity_out = await _run_async_tree(tree)
        values = {"capacity_kw": dtree_capacity_out.get("capacity_kw", None),
                  "capacity_hp": dtree_capacity_out.get("capacity_hp", None),}
        # tank_size = dtree_tank_out.get("tank_size", None)

        return values

    async def _check_backup(self, text, ref_number):
        logger.debug("Checking for generator backup for ref number %s", ref_number)
        tree = _setup_async_decision_tree(
            setup_graph_backup,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_backup_out = await _run_async_tree(tree)

        backup = dtree_backup_out.get("backup_mw", None)

        return backup
    
    async def _check_techs(self, text, ref_number):
        logger.debug("Checking for generator control techs for ref number %s", ref_number)
        tree = _setup_async_decision_tree(
            setup_graph_control_techs,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_control_techs_out = await _run_async_tree(tree)

        control_techs = dtree_control_techs_out.get("control_technologies", None)

        return control_techs
    
    async def _check_op_hours(self, text, ref_number):
        logger.debug("Checking for generator operating hours for ref number %s", ref_number)
        tree = _setup_async_decision_tree(
            setup_graph_operating_hours,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_operating_hours_out = await _run_async_tree(tree)

        operating_hours = dtree_operating_hours_out.get("operating_hours", None)

        return operating_hours


    async def _check_emissions(self, text, ref_number):
        logger.debug("Checking for generator emissions for ref number %s", ref_number)
        tree = _setup_async_decision_tree(
            setup_graph_emissions,
            text=text,
            ref_number=ref_number,
            chat_llm_caller=self._init_chat_llm_caller(DEFAULT_SYSTEM_MESSAGE),
        )
        dtree_emissions_out = await _run_async_tree(tree)

        emissions = dtree_emissions_out.get("emissions_limits", None)

        return emissions

