import geopandas as gpd
import logging
import math
import pandas as pd
import sys
from collections import defaultdict
from copy import deepcopy
from itertools import chain, compress, tee
from operator import attrgetter, itemgetter
from pathlib import Path
from shapely.geometry import MultiPoint, Point
from typing import List, Tuple

sys.path.insert(1, str(Path(__file__).resolve().parents[1]))
import helpers


# Set logger.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)


def ordered_pairs(coords: Tuple[tuple, ...]) -> List[Tuple[tuple, tuple]]:
    """
    Creates an ordered sequence of adjacent coordinate pairs, sorted.

    :param Tuple[tuple, ...] coords: tuple of coordinate tuples.
    :return List[Tuple[tuple, tuple]]: ordered sequence of coordinate pair tuples.
    """

    coords_1, coords_2 = tee(coords)
    next(coords_2, None)

    return sorted(zip(coords_1, coords_2))


class Validator:
    """Handles the execution of validation functions against the CRN dataset."""

    def __init__(self, crn: gpd.GeoDataFrame, dst: Path, layer: str) -> None:
        """
        Initializes variables for validation functions.

        :param gpd.GeoDataFrame crn: GeoDataFrame containing LineStrings.
        :param Path dst: output GeoPackage path.
        :param str layer: output GeoPackage layer name.
        """

        self.dst = dst
        self.layer = layer
        self.errors = defaultdict(list)
        self.id = "segment_id"

        # Standardize data and create subset of exclusively roads.
        self.crn = helpers.standardize(crn)
        self.crn_roads = self.crn.loc[self.crn["segment_type"] == 1].copy(deep=True)

        # Generate reusable geometry variables.
        self._gen_reusable_variables()

        logger.info("Configuring validations.")

        # Define validation.
        # Note: List validations in order if execution order matters.
        self.validations = {
            303: {"func": self.connectivity_segmentation,
                  "desc": "Arcs must not cross (i.e. must be segmented at each intersection)."},
            101: {"func": self.construction_singlepart,
                  "desc": "Arcs must be single part (i.e. \"LineString\")."},
            102: {"func": self.construction_min_length,
                  "desc": "Arcs must be >= 3 meters in length."},
            103: {"func": self.construction_simple,
                  "desc": "Arcs must be simple (i.e. must not self-overlap, self-cross, nor touch their interior)."},
            104: {"func": self.construction_cluster_tolerance,
                  "desc": "Arcs must have >= 0.01 meters distance between adjacent vertices (cluster tolerance)."},
            201: {"func": self.duplication_duplicated,
                  "desc": "Arcs must not be duplicated."},
            202: {"func": self.duplication_overlap,
                  "desc": "Arcs must not overlap (i.e. contain duplicated adjacent vertices)."},
            301: {"func": self.connectivity_node_intersection,
                  "desc": "Arcs must only connect at endpoints (nodes)."},
            302: {"func": self.connectivity_min_distance,
                  "desc": "Arcs must be >= 5 meters from each other, excluding connected arcs (i.e. no dangles)."}
        }

        # Define validation thresholds.
        self._min_len = 3
        self._min_dist = 5
        self._min_cluster_dist = 0.01

    def __call__(self) -> None:
        """Orchestrates the execution of validation functions and compiles the resulting errors."""

        try:

            # Iterate validations.
            for code, params in self.validations.items():
                func, description = itemgetter("func", "desc")(params)

                logger.info(f"Applying validation E{code}: \"{func.__name__}\".")

                # Execute validation and store non-empty results.
                results = func()
                if len(results["values"]):
                    self.errors[f"E{code} - {description}"] = deepcopy(results)

            # Export data.
            helpers.export(self.crn, dst=self.dst, name=self.layer)

        except (KeyError, SyntaxError, ValueError) as e:
            logger.exception("Unable to apply validation.")
            logger.exception(e)
            sys.exit(1)

    def _gen_reusable_variables(self) -> None:
        """Generates computationally intensive, reusable geometry attributes."""

        logger.info("Generating reusable geometry attributes.")

        # Generate computationally intensive geometry attributes as new columns.
        self.crn_roads["pts_tuple"] = self.crn_roads["geometry"].map(attrgetter("coords")).map(tuple)
        self.crn_roads["pt_start"] = self.crn_roads["pts_tuple"].map(itemgetter(0))
        self.crn_roads["pt_end"] = self.crn_roads["pts_tuple"].map(itemgetter(-1))
        self.crn_roads["pts_ordered_pairs"] = self.crn_roads["pts_tuple"].map(ordered_pairs)

        # Generate computationally intensive lookups.
        pts = self.crn_roads["pts_tuple"].explode()
        pts_df = pd.DataFrame({"pt": pts.values, self.id: pts.index})
        self.pts_id_lookup = helpers.groupby_to_list(pts_df, "pt", self.id).map(set).to_dict()
        self.idx_id_lookup = dict(zip(range(len(self.crn_roads)), self.crn_roads.index))

    def connectivity_min_distance(self) -> dict:
        """
        Validation: Arcs must be >= 5 meters from each other, excluding connected arcs (i.e. no dangles).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Compile all non-duplicated nodes (dead ends) as a DataFrame.
        pts = self.crn_roads["pt_start"].append(self.crn_roads["pt_end"])
        deadends = pts.loc[~pts.duplicated(keep=False)]
        deadends = pd.DataFrame({"pt": deadends.values, self.id: deadends.index})

        # Generate simplified node buffers with distance tolerance.
        deadends["buffer"] = deadends["pt"].map(lambda pt: Point(pt).buffer(self._min_dist, resolution=5))

        # Query arcs which intersect each dead end buffer.
        deadends["intersects"] = deadends["buffer"].map(
            lambda buffer: set(self.crn_roads.sindex.query(buffer, predicate="intersects")))

        # Flag dead ends which have buffers with one or more intersecting arcs.
        deadends = deadends.loc[deadends["intersects"].map(len) > 1]
        if len(deadends):

            # Aggregate deadends to their source features.
            # Note: source features will exist twice if both nodes are deadends; these results will be aggregated.
            deadends_agg = helpers.groupby_to_list(deadends, self.id, "intersects")\
                .map(chain.from_iterable).map(set).to_dict()
            deadends["intersects"] = deadends[self.id].map(deadends_agg)
            deadends.drop_duplicates(subset=self.id, inplace=True)

            # Compile identifiers corresponding to each 'intersects' index.
            deadends["intersects"] = deadends["intersects"].map(lambda idxs: set(itemgetter(*idxs)(self.idx_id_lookup)))

            # Compile identifiers containing either of the source geometry nodes.
            deadends["connected"] = deadends[self.id].map(
                lambda identifier: set(chain.from_iterable(
                    itemgetter(node)(self.pts_id_lookup) for node in itemgetter(0, -1)(
                        itemgetter(identifier)(self.crn_roads["pts_tuple"]))
                )))

            # Subtract identifiers of connected features from buffer-intersecting features.
            deadends["disconnected"] = deadends["intersects"] - deadends["connected"]

            # Filter to those results with disconnected arcs.
            flag = deadends["disconnected"].map(len) > 0
            if sum(flag):

                # Remove duplicated results.
                deadends = deadends.loc[flag]
                deadends["ids"] = deadends[[self.id, "disconnected"]].apply(
                    lambda row: tuple({row[0], *row[1]}), axis=1)
                deadends.drop_duplicates(subset="ids", keep="first", inplace=True)

                # Compile error logs.
                errors["values"] = deadends["ids"].map(
                    lambda ids: f"Disconnected features are too close: {*ids,}".replace(",)", ")")).to_list()
                vals = set(chain.from_iterable(deadends["ids"]))
                errors["query"] = f"\"{self.id}\" in {*vals,}".replace(",)", ")")

        return errors

    def connectivity_node_intersection(self) -> dict:
        """
        Validates: Arcs must only connect at endpoints (nodes).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Compile nodes.
        nodes = set(self.crn_roads["pt_start"].append(self.crn_roads["pt_end"]))

        # Compile interior vertices (non-nodes).
        # Note: only arcs with > 2 vertices are used.
        non_nodes = set(self.crn_roads.loc[self.crn_roads["pts_tuple"].map(len) > 2, "pts_tuple"]
                        .map(lambda pts: set(pts[1:-1])).map(tuple).explode())

        # Compile invalid vertices.
        invalid_pts = nodes.intersection(non_nodes)

        # Filter invalid vertices to those with multiple connected features.
        invalid_pts = set(compress(invalid_pts,
                                   map(lambda pt: len(itemgetter(pt)(self.pts_id_lookup)) > 1, invalid_pts)))
        if len(invalid_pts):

            # Filter arcs to those with an invalid vertex.
            invalid_ids = set(chain.from_iterable(map(lambda pt: itemgetter(pt)(self.pts_id_lookup), invalid_pts)))
            crn_roads = self.crn_roads.loc[self.crn_roads.index.isin(invalid_ids)]

            # Flag invalid arcs where the invalid vertex is a non-node.
            flag = crn_roads["pts_tuple"].map(lambda pts: len(set(pts[1:-1]).intersection(invalid_pts))) > 0
            if sum(flag):

                # Compile error logs.
                vals = set(crn_roads.loc[flag].index)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in {*vals,}".replace(",)", ")")

        return errors

    def connectivity_segmentation(self) -> dict:
        """
        Validates: Arcs must not cross (i.e. must be segmented at each intersection).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Query arcs which cross each arc.
        crosses = self.crn_roads["geometry"].map(lambda g: set(self.crn_roads.sindex.query(g, predicate="crosses")))

        # Flag arcs which have one or more crossing arcs.
        flag = crosses.map(len) > 0
        if sum(flag):

            # Compile error logs.
            vals = set(self.crn_roads.loc[flag].index)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in {*vals,}".replace(",)", ")")

        return errors

    def construction_cluster_tolerance(self) -> dict:
        """
        Validates: Arcs must have >= 1x10-2 (0.01) meters distance between adjacent vertices (cluster tolerance).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Filter arcs to those with > 2 vertices.
        crn_roads = self.crn_roads.loc[self.crn_roads["pts_tuple"].map(len) > 2]
        if len(crn_roads):

            # Explode arc coordinate pairs and calculate distances.
            coord_pairs = crn_roads["pts_ordered_pairs"].explode()
            coord_dist = coord_pairs.map(lambda pair: math.dist(*pair))

            # Flag pairs with distances that are too small.
            flag = coord_dist < self._min_cluster_dist
            if sum(flag):

                # Export invalid pairs as MultiPoint geometries.
                pts = coord_pairs.loc[flag].map(MultiPoint)
                pts_df = gpd.GeoDataFrame({self.id: pts.index.values}, geometry=[*pts], crs=self.crn_roads.crs)

                logger.info(f"Writing to file: {self.dst.name}|layer={self.layer}_cluster_tolerance")
                pts_df.to_file(str(self.dst), driver="GPKG", layer=f"{self.layer}_cluster_tolerance")

                # Compile error logs.
                vals = set(coord_pairs.loc[flag].index)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in {*vals,}".replace(",)", ")")

        return errors

    def construction_min_length(self) -> dict:
        """
        Validates: Arcs must be >= 3 meters in length, except structures (e.g. Bridges).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Flag arcs which are too short.
        flag = self.crn_roads.length < self._min_len
        if sum(flag):
            
            # Flag isolated structures (structures not connected to another structure).
            
            # Compile structures.
            structures = self.crn_roads.loc[~self.crn_roads["structure_type"].isin({"Unknown", "None"})]
            
            # Compile duplicated structure nodes.
            structure_nodes = pd.Series(structures["pt_start"].append(structures["pt_end"]))
            structure_nodes_dups = set(structure_nodes.loc[structure_nodes.duplicated(keep=False)])
            
            # Flag isolated structures.
            isolated_structure_index = set(structures.loc[~((structures["pt_start"].isin(structure_nodes_dups)) |
                                                            (structures["pt_end"].isin(structure_nodes_dups)))].index)
            isolated_structure_flag = self.crn_roads.index.isin(isolated_structure_index)
            
            # Modify flag to exclude isolated structures.
            flag = (flag & (~isolated_structure_flag))
            if sum(flag):

                # Compile error logs.
                vals = set(self.crn_roads.loc[flag].index)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in {*vals,}".replace(",)", ")")

        return errors

    def construction_simple(self) -> dict:
        """
        Validates: Arcs must be simple (i.e. must not self-overlap, self-cross, nor touch their interior).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Flag complex (non-simple) geometries.
        flag = ~self.crn_roads.is_simple
        if sum(flag):

            # Compile error logs.
            vals = set(self.crn_roads.loc[flag].index)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in {*vals,}".replace(",)", ")")

        return errors

    def construction_singlepart(self) -> dict:
        """
        Validates: Arcs must be single part (i.e. 'LineString').

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Flag non-LineStrings.
        flag = self.crn_roads.geom_type != "LineString"
        if sum(flag):

            # Compile error logs.
            vals = set(self.crn_roads.loc[flag].index)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in {*vals,}".replace(",)", ")")

        return errors

    def duplication_duplicated(self) -> dict:
        """
        Validates: Arcs must not be duplicated.

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Filter arcs to those with duplicated lengths.
        crn_roads = self.crn_roads.loc[self.crn_roads.length.duplicated(keep=False)]
        if len(crn_roads):

            # Filter arcs to those with duplicated nodes.
            crn_roads = crn_roads.loc[
                crn_roads[["pt_start", "pt_end"]].agg(set, axis=1).map(tuple).duplicated(keep=False)]

            # Flag duplicated geometries.
            dups = crn_roads.loc[crn_roads["geometry"].map(
                lambda g1: crn_roads["geometry"].map(lambda g2: g1.equals(g2)).sum() > 1)]
            if len(dups):

                # Compile error logs.
                vals = set(dups.index)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in {*vals,}".replace(",)", ")")

        return errors

    def duplication_overlap(self) -> dict:
        """
        Validates: Arcs must not overlap (i.e. contain duplicated adjacent vertices).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Query arcs which overlap each arc.
        overlaps = self.crn_roads["geometry"].map(lambda g: set(self.crn_roads.sindex.query(g, predicate="overlaps")))

        # Flag arcs which have one or more overlapping arcs.
        flag = overlaps.map(len) > 0

        # Compile error logs.
        if sum(flag):
            vals = set(overlaps.loc[flag].index)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in {*vals,}".replace(",)", ")")

        return errors
