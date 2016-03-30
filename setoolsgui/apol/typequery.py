# Copyright 2016, Tresys Technology, LLC
#
# This file is part of SETools.
#
# SETools is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 2.1 of
# the License, or (at your option) any later version.
#
# SETools is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with SETools.  If not, see
# <http://www.gnu.org/licenses/>.
#

import logging

from PyQt5.QtCore import pyqtSignal, Qt, QObject, QSortFilterProxyModel, QStringListModel, QThread
from PyQt5.QtGui import QPalette, QTextCursor
from PyQt5.QtWidgets import QCompleter, QHeaderView, QMessageBox, QProgressDialog, QScrollArea
from setools import TypeQuery

from ..logtosignal import LogHandlerToSignal
from ..models import SEToolsListModel, invert_list_selection
from ..typemodel import TypeTableModel, type_detail
from ..widget import SEToolsWidget


class TypeQueryTab(SEToolsWidget, QScrollArea):

    """Type browser and query tab."""

    def __init__(self, parent, policy, perm_map):
        super(TypeQueryTab, self).__init__(parent)
        self.log = logging.getLogger(__name__)
        self.policy = policy
        self.query = TypeQuery(policy)
        self.setupUi()

    def __del__(self):
        self.thread.quit()
        self.thread.wait(5000)
        logging.getLogger("setools.typequery").removeHandler(self.handler)

    def setupUi(self):
        self.load_ui("typequery.ui")

        # populate type list
        self.type_model = SEToolsListModel(self)
        self.type_model.item_list = sorted(self.policy.types())
        self.types.setModel(self.type_model)

        # populate attribute list
        self.attr_model = SEToolsListModel(self)
        self.attr_model.item_list = sorted(self.policy.typeattributes())
        self.attrs.setModel(self.attr_model)

        # set up results
        self.table_results_model = TypeTableModel(self)
        self.sort_proxy = QSortFilterProxyModel(self)
        self.sort_proxy.setSourceModel(self.table_results_model)
        self.table_results.setModel(self.sort_proxy)

        # setup indications of errors on level/range
        self.orig_palette = self.name.palette()
        self.error_palette = self.name.palette()
        self.error_palette.setColor(QPalette.Base, Qt.red)
        self.clear_name_error()

        # set up processing thread
        self.thread = QThread()
        self.worker = ResultsUpdater(self.query, self.table_results_model)
        self.worker.moveToThread(self.thread)
        self.worker.raw_line.connect(self.raw_results.appendPlainText)
        self.worker.finished.connect(self.update_complete)
        self.worker.finished.connect(self.thread.quit)
        self.thread.started.connect(self.worker.update)

        # create a "busy, please wait" dialog
        self.busy = QProgressDialog(self)
        self.busy.setModal(True)
        self.busy.setRange(0, 0)
        self.busy.setMinimumDuration(0)
        self.busy.canceled.connect(self.thread.requestInterruption)
        self.busy.reset()

        # update busy dialog from query INFO logs
        self.handler = LogHandlerToSignal()
        self.handler.message.connect(self.busy.setLabelText)
        logging.getLogger("setools.typequery").addHandler(self.handler)

        # Ensure settings are consistent with the initial .ui state
        self.notes.setHidden(not self.notes_expander.isChecked())

        # connect signals
        self.types.doubleClicked.connect(self.get_detail)
        self.types.get_detail.triggered.connect(self.get_detail)
        self.name.textEdited.connect(self.clear_name_error)
        self.name.editingFinished.connect(self.set_name)
        self.name_regex.toggled.connect(self.set_name_regex)
        self.attrs.selectionModel().selectionChanged.connect(self.set_attrs)
        self.invert_attrs.clicked.connect(self.invert_attr_selection)
        self.buttonBox.clicked.connect(self.run)

    #
    # Type browser
    #
    def get_detail(self):
        # .ui is set for single item selection.
        index = self.types.selectedIndexes()[0]
        item = self.type_model.data(index, Qt.UserRole)

        self.log.debug("Generating detail window for {0}".format(item))
        type_detail(self, item)

    #
    # Name criteria
    #
    def clear_name_error(self):
        self.name.setToolTip("Match the type name.")
        self.name.setPalette(self.orig_palette)

    def set_name(self):
        try:
            self.query.name = self.name.text()
        except Exception as ex:
            self.log.error("Type name error: {0}".format(ex))
            self.name.setToolTip("Error: " + str(ex))
            self.name.setPalette(self.error_palette)

    def set_name_regex(self, state):
        self.log.debug("Setting name_regex {0}".format(state))
        self.query.name_regex = state
        self.clear_name_error()
        self.set_name()

    #
    # Attribute criteria
    #
    def set_attrs(self):
        selected_attrs = []
        for index in self.attrs.selectionModel().selectedIndexes():
            selected_attrs.append(self.attr_model.data(index, Qt.UserRole))

        self.query.attrs = selected_attrs

    def invert_attr_selection(self):
        invert_list_selection(self.attrs.selectionModel())

    #
    # Results runner
    #

    def run(self, button):
        # right now there is only one button.
        self.query.name_regex = self.name_regex.isChecked()
        self.query.attrs_equal = self.attrs_equal.isChecked()
        self.query.permissive = self.permissive.isChecked()

        # start processing
        self.busy.setLabelText("Processing query...")
        self.busy.show()
        self.raw_results.clear()
        self.thread.start()

    def update_complete(self):
        # update sizes/location of result displays
        if not self.busy.wasCanceled():
            self.busy.setLabelText("Resizing the result table's columns; GUI may be unresponsive")
            self.busy.repaint()
            self.table_results.resizeColumnsToContents()
            # If the attrs or alias column widths are too long, pull back
            # to a reasonable size
            header = self.table_results.horizontalHeader()
            if header.sectionSize(1) > 400:
                header.resizeSection(1, 400)
            if header.sectionSize(2) > 400:
                header.resizeSection(2, 400)

        if not self.busy.wasCanceled():
            self.busy.setLabelText("Resizing the result table's rows; GUI may be unresponsive")
            self.busy.repaint()
            self.table_results.resizeRowsToContents()

        if not self.busy.wasCanceled():
            self.busy.setLabelText("Moving the raw result to top; GUI may be unresponsive")
            self.busy.repaint()
            self.raw_results.moveCursor(QTextCursor.Start)

        self.busy.reset()


class ResultsUpdater(QObject):

    """
    Thread for processing queries and updating result widgets.

    Parameters:
    query       The query object
    model       The model for the results

    Qt signals:
    finished    The update has completed.
    raw_line    (str) A string to be appended to the raw results.
    """

    finished = pyqtSignal()
    raw_line = pyqtSignal(str)

    def __init__(self, query, model):
        super(ResultsUpdater, self).__init__()
        self.query = query
        self.log = logging.getLogger(__name__)
        self.table_results_model = model

    def update(self):
        """Run the query and update results."""
        self.table_results_model.beginResetModel()

        results = []
        counter = 0

        for counter, item in enumerate(self.query.results(), start=1):
            results.append(item)

            self.raw_line.emit(item.statement())

            if QThread.currentThread().isInterruptionRequested():
                break
            elif not counter % 10:
                # yield execution every 10 rules
                QThread.yieldCurrentThread()

        self.table_results_model.resultlist = results
        self.table_results_model.endResetModel()

        self.log.info("{0} type(s) found.".format(counter))

        self.finished.emit()