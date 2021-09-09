import geopandas as gpd
import logging
import networkx as nx
import numpy as np
import pandas as pd
import shapely.ops
import string
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from itertools import chain, combinations, compress, groupby, product, tee
from operator import attrgetter, itemgetter
from pathlib import Path
from scipy.spatial import cKDTree
from scipy.spatial.distance import euclidean
from shapely.geometry import Point
from typing import Dict, List, Tuple, Union

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
    """Handles the execution of validation functions against the segment dataset."""

    def __init__(self, segment: gpd.GeoDataFrame) -> None:
        """
        Initializes variables for validation functions.

        :param gpd.GeoDataFrame segment: GeoDataFrame containing LineStrings.
        """

        self.segment = segment.copy(deep=True)
        self.errors = defaultdict(list)
        self.id = "segment_id"

        logger.info("Validating segment identifiers.")
        # Performs the following identifier validations:
        # 1) identifiers must be 32 digit hexadecimal strings.
        # 2) identifiers must be unique (nulls excluded from this validation).
        self.segment[self.id] = self.segment[self.id].astype(str)

        try:

            hexdigits = set(string.hexdigits)
            flag_non_hex = (self.segment[self.id].map(len) != 32) | \
                           (self.segment[self.id].map(lambda val: not set(val).issubset(hexdigits)))
            flag_dups = (self.segment[self.id].duplicated(keep=False)) & (self.segment[self.id] != "None")

            # Log invalid segment identifiers.
            if sum(flag_non_hex) or sum(flag_dups):
                logger.exception(f"Invalid identifiers detected for \"{self.id}\".")
                if sum(flag_non_hex):
                    values = "\n".join(self.segment.loc[flag_non_hex, self.id].drop_duplicates(keep="first").values)
                    logger.exception(f"Validation: identifiers must be 32 digit hexadecimal strings.\n{values}")
                if sum(flag_dups):
                    values = "\n".join(self.segment.loc[flag_dups, self.id].drop_duplicates(keep="first").values)
                    logger.exception(f"Validation: identifiers must be unique.\n{values}")
                sys.exit(1)

        except ValueError as e:
            logger.exception(f"Unable to validate segment identifiers for \"{self.id}\".")
            logger.exception(e)
            sys.exit(1)

        logger.info("Standardizing segments.")

        # Performs the following data standardizations:
        # 1) exclude non-road segments (segment_type=1).
        # 2) re-projects to a meter-based projection (EPSG:3348).
        # 3) explodes multi-part geometries.
        # 4) flattens coordinates to 2-dimensions.
        # 5) rounds coordinates to 7 decimal places.
        self.segment = self.segment.loc[self.segment.segment_type.astype(int) == 1]
        self.segment = self.segment.to_crs("EPSG:3348")
        self.segment = helpers.explode_geometry(self.segment)
        self.segment = helpers.flatten_coordinates(self.segment)
        self.segment = helpers.round_coordinates(self.segment, precision=7)

        # Set identifiers as index and drop unneeded columns.
        self.segment.index = self.segment[self.id]
        self.segment = self.segment[[self.id, "geometry"]].copy(deep=True)

        logger.info("Configuring validations.")

        # Define validation.
        # Note: List validations in order if execution order matters.
        self.validations = {
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
                  "desc": "Arcs must be >= 5 meters from each other, excluding connected arcs (i.e. no dangles)."},
            303: {"func": self.connectivity_segmentation,
                  "desc": "Arcs must not cross (i.e. must be segmented at each intersection)."}
        }

        logger.info("Generating reusable geometry attributes.")

        # Store computationally intensive geometry attributes as new dataframe columns.
        self.segment["pts_tuple"] = self.segment["geometry"].map(attrgetter("coords")).map(tuple)
        self.segment["pts_set"] = self.segment["pts_tuple"].map(set)
        self.segment["pt_start"] = self.segment["pts_tuple"].map(itemgetter(0))
        self.segment["pt_end"] = self.segment["pts_tuple"].map(itemgetter(-1))
        self.segment["pts_ordered_pairs"] = self.segment["pts_tuple"].map(ordered_pairs)

        # Store computationally intensive lookups.
        pts = self.segment["pts_tuple"].explode()
        pts_df = pd.DataFrame(pts).reset_index(drop=False)
        self.pts_id_lookup = helpers.groupby_to_list(pts_df, "pts_tuple", self.id).map(set).to_dict()
        self.pts_idx_lookup = dict(zip(pts_df.index, pts_df[self.id]))
        self.pts_unique = set(pts_df.loc[~pts_df["pts_tuple"].duplicated(keep=False), "pts_tuple"])

    def connectivity_min_distance(self) -> dict:
        """
        Validation: Arcs must be >= 5 meters from each other, excluding connected arcs (i.e. no dangles).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # TODO

        return errors

    def connectivity_node_intersection(self) -> dict:
        """
        Validates: Arcs must only connect at endpoints (nodes).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # TODO

        return errors

    def connectivity_segmentation(self) -> dict:
        """
        Validates: Arcs must not cross (i.e. must be segmented at each intersection).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # TODO

        return errors

    def construction_cluster_tolerance(self) -> dict:
        """
        Validates: Arcs must have >= 1x10-2 (0.01) meters distance between adjacent vertices (cluster tolerance).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Filter segments to those with > 2 vertices.
        segment = self.segment.loc[self.segment["pts_tuple"].map(len) > 2]
        if len(segment):

            # Explode segment coordinate pairs and calculate distances.
            coord_pairs = segment["pts_ordered_pairs"].explode()
            coord_dist = coord_pairs.map(lambda pair: euclidean(*pair))

            # Flag pairs with distances that are too small.
            min_distance = 0.01
            flag = coord_dist < min_distance

            # Compile error logs.
            if sum(flag):
                vals = set(coord_pairs.loc[flag].index)
                vals_quotes = map(lambda val: f"'{val}'", vals)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in ({','.join(vals_quotes)})"

        return errors

    def construction_min_length(self) -> dict:
        """
        Validates: Arcs must be >= 3 meters in length.

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Flag arcs which are too short.
        min_length = 3
        flag = self.segment < min_length

        # Compile error logs.
        if sum(flag):
            vals = self.segment.loc[flag, self.id].values
            vals_quotes = map(lambda val: f"'{val}'", vals)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in ({','.join(vals_quotes)})"

        return errors

    def construction_simple(self) -> dict:
        """
        Validates: Arcs must be simple (i.e. must not self-overlap, self-cross, nor touch their interior).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Flag complex (non-simple) geometries.
        flag = ~self.segment.is_simple

        # Compile error logs.
        if sum(flag):
            vals = self.segment.loc[flag, self.id].values
            vals_quotes = map(lambda val: f"'{val}'", vals)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in ({','.join(vals_quotes)})"

        return errors

    def construction_singlepart(self) -> dict:
        """
        Validates: Arcs must be single part (i.e. 'LineString').

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Flag non-LineStrings.
        flag = self.segment.geom_type != "LineString"

        # Compile error logs.
        if sum(flag):
            vals = self.segment.loc[flag, self.id].values
            vals_quotes = map(lambda val: f"'{val}'", vals)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in ({','.join(vals_quotes)})"

        return errors

    def duplication_duplicated(self) -> dict:
        """
        Validates: Arcs must not be duplicated.

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Filter segments to those with duplicated lengths.
        segment = self.segment.loc[self.segment.length.duplicated(keep=False)]
        if len(segment):

            # Filter segments to those with duplicated nodes.
            segment = segment.loc[segment[["pt_start", "pt_end"]].agg(set, axis=1).map(tuple).duplicated(keep=False)]

            # Flag duplicated geometries.
            dups = segment.loc[segment["geometry"].map(
                lambda g1: segment["geometry"].map(lambda g2: g1.equals(g2)).sum() > 1)]
            if len(dups):

                # Compile error logs.
                vals = dups[self.id].values
                vals_quotes = map(lambda val: f"'{val}'", vals)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in ({','.join(vals_quotes)})"

        return errors

    def duplication_overlap(self) -> dict:
        """
        Validates: Arcs must not overlap (i.e. contain duplicated adjacent vertices).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # TODO

        return errors

    def execute(self) -> None:
        """Orchestrates the execution of validation functions and compiles the resulting errors."""

        try:

            # Iterate validations.
            for code, params in self.validations.items():
                func, description = itemgetter("func", "desc")(params)

                logger.info(f"Applying validation: \"{func.__name__}\".")

                # Execute validation and store non-empty results.
                results = func()
                if len(results["values"]):
                    self.errors[f"E{code} - {description}"] = deepcopy(results)

        except (KeyError, SyntaxError, ValueError) as e:
            logger.exception("Unable to apply validation.")
            logger.exception(e)
            sys.exit(1)
