# This file is Copyright 2019 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#
import logging
from typing import List

from volatility.framework import constants, exceptions, renderers, interfaces
from volatility.framework.configuration import requirements
from volatility.framework.objects import utility
from volatility.plugins.windows import pslist

vollog = logging.getLogger(__name__)


class CmdLine(interfaces.plugins.PluginInterface):
    """Lists process command line arguments."""

    @classmethod
    def get_requirements(cls) -> List[interfaces.configuration.RequirementInterface]:
        # Since we're calling the plugin, make sure we have the plugin's requirements
        return [
            requirements.TranslationLayerRequirement(name = 'primary',
                                                     description = 'Memory layer for the kernel',
                                                     architectures = ["Intel32", "Intel64"]),
            requirements.SymbolTableRequirement(name = "nt_symbols", description = "Windows kernel symbols"),
            requirements.PluginRequirement(name = 'pslist', plugin = pslist.PsList, version = (1, 0, 0)),
            requirements.ListRequirement(name = 'pid',
                                         element_type = int,
                                         description = "Process IDs to include (all other processes are excluded)",
                                         optional = True)
        ]

    @classmethod
    def get_cmdline(self, context: interfaces.context.ContextInterface, 
                    kernel_table_name: str, proc):
        """Extracts the cmdline from PEB

        Args:
            context: the context to operate upon
            kernel_table_name: the name for the symbol table containing the kernel's symbols
            proc: the process object

        Returns:
            A string with the command line
        """
        try:
            proc_id = proc.UniqueProcessId
            proc_layer_name = proc.add_process_layer()

        except exceptions.InvalidAddressException as excp:
            vollog.debug("Process {}: invalid address {} in layer {}".format(proc_id, excp.invalid_address,
                                                                             excp.layer_name))
            return "Invalid address for PID"

        try:
            peb = context.object(kernel_table_name + constants.BANG + "_PEB",
                                       layer_name = proc_layer_name,
                                       offset = proc.Peb)
            result_text = peb.ProcessParameters.CommandLine.get_string()

        except exceptions.SwappedInvalidAddressException as exp:
            result_text = "Required memory at {0:#x} is inaccessible (swapped)".format(exp.invalid_address)

        except exceptions.PagedInvalidAddressException as exp:
            result_text = "Required memory at {0:#x} is not valid (process exited?)".format(exp.invalid_address)

        except exceptions.InvalidAddressException as exp:
            result_text = "Required memory at {0:#x} is not valid (incomplete layer {1}?)".format(
                exp.invalid_address, exp.layer_name)

        return result_text


    def _generator(self, procs):

        for proc in procs:
            process_name = utility.array_to_string(proc.ImageFileName)
            proc_id = "Unknown"

            result_text = self.get_cmdline(self.context, self.config["nt_symbols"], proc)

            yield (0, (proc.UniqueProcessId, process_name, result_text))

    def run(self):
        filter_func = pslist.PsList.create_pid_filter(self.config.get('pid', None))

        return renderers.TreeGrid([("PID", int), ("Process", str), ("Args", str)],
                                  self._generator(
                                      pslist.PsList.list_processes(context = self.context,
                                                                   layer_name = self.config['primary'],
                                                                   symbol_table = self.config['nt_symbols'],
                                                                   filter_func = filter_func)))
