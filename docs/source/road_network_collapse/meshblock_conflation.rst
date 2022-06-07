********************
Meshblock Conflation
********************

.. contents:: Contents:
   :depth: 5

Overview
========

The NGD network needs to be linkable to the new CRN network. This includes individual arcs and BB (meshblock) units.

The CRN and NGD network differences are not limited to the quantity of arcs, but also the shape and alignment of those
arcs. This is why the networks cannot be simply linked arc-to-arc, but require more detailed conflation to reduce
alignment differences. A threshold of 80% is currently used to determine acceptable network alignment and classify
individual meshblock polygons (BBs) as ``conflated`` or ``unconflated``.

.. admonition:: Definition

    ``conflated``: An NGD BB which has >= 80% of its area contained within a single EGP BB.

.. admonition:: Threshold Strictness

    It will not always be possible to achieve the conflation threshold without introducing needless complexity to the
    CRN. Therefore, the threshold should be treated as a soft restriction and lower values are acceptable so long as
    the NGD BB can be guaranteed to link to a single EGP BB based on majority area occupation, as defined above for
    ``conflation``.

The required actions to resolve ``unconflated`` BBs should follow those outlined in the various validations in
:doc:`meshblock_creation`.

Resources
---------

:CLI Tool: ``egp/src/conflation/conflate_meshblock.py``
:Output: - Basic metrics output to console.
         - Updated source layer: ``egp/data/egp_data.gpkg|layer=nrn_bo_<source>``
         - New (EGP) BB layer: ``egp/data/egp_data.gpkg|layer=<source>_meshblock``
         - Current (NGD) BB layer: ``egp/data/egp_data.gpkg|layer=<source>_meshblock_ngd``
:Editing Environment: ``egp/data/egp_editing_meshblock_conflation.qgz``

Editing Process
---------------

.. figure:: /source/_static/meshblock_conflation/editing_process_meshblock_conflation.png
    :alt: Editing process overview.

    Figure 1: Editing process overview.

Cardinalities
-------------

.. admonition:: Definition

    Cardinalities: In a database context, cardinalities refer to the numerical relationships between records of two or
    more datasets.

.. figure:: /source/_static/meshblock_conflation/cardinalities_overview.png
    :alt: Cardinalities overview.

    Figure 2: Cardinalities overview. Direction: EGP (blue) - to - NGD (pink).

**Cardinalities Explained:**

``one-to-one``:
    :Explanation: Networks align.
    :Action: None.
``many-to-one``:
    :Explanation: CRN is more detailed.
    :Action: None.
``one-to-many``:
    :Explanation: NGD is more detailed.
    :Action: Create new BOs and / or copy missing NGD roads.
``many-to-many``:
    :Explanation: Networks conflict.
    :Action: Create new BOs.

QGIS Project
------------

Explanation of Layers
^^^^^^^^^^^^^^^^^^^^^

.. figure:: /source/_static/meshblock_conflation/qgis_project_layers.png
    :alt: QGIS table of contents.

    Figure 3: QGIS table of contents.

**Layers:**

:``nrn_bo``: Primary editing layer representing NRN roads and ferries, NGD BOs, and added NGD roads.
:``ngd_road``: NGD roads (non-BOs) to be used for identifying roads which are missing from the NRN and are required for
               conflation.
:``meshblock_ngd``: NGD BB layer, classified by conflation validity status and labelled with a conflation percentage.
:``meshblock``: EGP BB layer generated from ``nrn_bo``.
:``Esri Satellite``: Reference layer for recent imagery context.

Example Output
^^^^^^^^^^^^^^

.. figure:: /source/_static/meshblock_conflation/qgis_example_output.png
    :alt: QGIS example output.

    Figure 4: QGIS example output.

Conflation Scenarios
====================

.. admonition:: Converting NGD roads to BOs

    If an NGD road needs to be converted to a BO, copy and paste the NGD feature(s) into the CRN data and set
    ``bo_new=1``. The script will automatically set ``segment_type=3`` for these features, or you can do it yourself.

.. admonition:: Adding new BOs (completely new arcs)

    If a network difference prevents an EGP BB from being properly conflated, you may need to add a new arc to the CRN
    data. After creating the arc, either set ``bo_new=1``, ``segment_type=3``, or both. The script will automatically
    resolve the other attribute if only one of them is set.

Scenario: Missing NGD Roads
---------------------------

.. include:: /source/_static/meshblock_conflation/scenario_missing_ngd_roads.rst

Scenario: Missing False NGD Road (1)
------------------------------------

.. include:: /source/_static/meshblock_conflation/scenario_missing_false_ngd_road_1.rst

Scenario: Missing False NGD Road (2)
------------------------------------

.. include:: /source/_static/meshblock_conflation/scenario_missing_false_ngd_road_2.rst

Scenario: Misaligned Networks
-----------------------------

.. admonition:: Note

    Misaligned networks are difficult to resolve since there is no clear nor obvious solution. If the EGP and NGD BBs
    are not too different in shape, try modifying the BO shape slightly or rerunning the script with a slightly lower
    threshold value. Larger differences may require adding NGD roads as BOs and / or adding completely new BOs, thereby
    segmenting the data until the conflation threshold is satisfied.

Scenario: Misaligned Networks (1)
"""""""""""""""""""""""""""""""""

.. include:: /source/_static/meshblock_conflation/scenario_misaligned_networks_1.rst

Scenario: Misaligned Networks (2)
"""""""""""""""""""""""""""""""""

.. include:: /source/_static/meshblock_conflation/scenario_misaligned_networks_2.rst

Scenario: Misaligned Networks (3)
"""""""""""""""""""""""""""""""""

.. include:: /source/_static/meshblock_conflation/scenario_misaligned_networks_3.rst

Scenario: Misaligned Networks - Additional Examples
---------------------------------------------------

.. include:: /source/_static/meshblock_conflation/scenario_misaligned_networks_additional_examples.rst

Scenario: Isolated Blocks
-------------------------

.. include:: /source/_static/meshblock_conflation/scenario_isolated_blocks.rst

Scenario: Correction of NGD Road
--------------------------------

.. include:: /source/_static/meshblock_conflation/scenario_correction_of_ngd_road.rst

Progress
========

This section is temporary and will be removed once this task is completed.

.. figure:: /source/_static/progress_charts/meshblock_conflation_progress.svg
    :alt: Meshblock conflation progress.

    Figure 5: Meshblock conflation progress - original and current number of unconflated NGD BBs as of May 30, 2022.
