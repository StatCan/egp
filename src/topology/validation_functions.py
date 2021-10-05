import geopandas as gpd
import logging
import pandas as pd
import string
import sys
import uuid
from collections import defaultdict
from copy import deepcopy
from functools import reduce
from itertools import chain, tee
from operator import attrgetter, itemgetter
from pathlib import Path
from scipy.spatial.distance import euclidean
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import split
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


def split_line(line1: LineString, line2: LineString) -> MultiLineString:
    """
    Splits a LineString based on a crossing LineString.

    :param LineString line1: LineString to be split.
    :param LineString line2: LineString to be used as the splitter.
    :return MultiLineString: Segmented, MultiLineString, version of the original LineString.
    """

    return MultiLineString(split(line1, line2))


class Validator:
    """Handles the execution of validation functions against the segment dataset."""

    def __init__(self, segment: gpd.GeoDataFrame, dst: Path, layer: str) -> None:
        """
        Initializes variables for validation functions.

        :param gpd.GeoDataFrame segment: GeoDataFrame containing LineStrings.
        :param Path dst: output GeoPackage path.
        :param str layer: output GeoPackage layer name.
        """

        self.segment = segment.copy(deep=True)
        self.segment_original = None
        self.dst = dst
        self.layer = layer
        self.errors = defaultdict(list)
        self.id = "segment_id"

        logger.info(f"Resolving segment identifiers for: \"{self.id}\".")
        # Identifiers must be unique 32 digit hexadecimal strings. The process of data editing is likely to create
        # duplicated or invalid identifiers; this process will resolve any invalid values such that the script can
        # proceed. This process will overwrite any invalid identifiers. Therefore, original identifiers should be
        # stored in a separate, backup column for post-processing linkages.
        self.segment[self.id] = self.segment[self.id].astype(str)

        try:

            # Flag invalid identifiers.
            hexdigits = set(string.hexdigits)
            flag_non_hex = (self.segment[self.id].map(len) != 32) | \
                           (self.segment[self.id].map(lambda val: not set(val).issubset(hexdigits)))
            flag_dups = (self.segment[self.id].duplicated(keep=False)) & (self.segment[self.id] != "None")
            flag_invalid = flag_non_hex | flag_dups

            # Resolve invalid identifiers.
            if sum(flag_invalid):
                logger.warning(f"Resolving {sum(flag_invalid)} invalid identifiers for: \"{self.id}\".")

                # Overwrite identifiers and export updated layer to file.
                self.segment.loc[flag_invalid, self.id] = [uuid.uuid4().hex for _ in range(sum(flag_invalid))]
                helpers.export(self.segment, dst=self.dst, name=self.layer)

        except ValueError as e:
            logger.exception(f"Unable to validate segment identifiers for \"{self.id}\".")
            logger.exception(e)
            sys.exit(1)

        logger.info("Standardizing segments.")

        self.segment_original = self.segment.copy(deep=True)

        # Performs the following data standardizations:
        # 1) exclude non-road segments (segment_type=1).
        # 2) re-projects to a meter-based projection (EPSG:3348).
        # 3) explodes multi-part geometries.
        # 4) rounds coordinates to 7 decimal places and flattens to 2-dimensions.
        self.segment = self.segment.loc[self.segment.segment_type.astype(int) == 1]
        self.segment = self.segment.to_crs("EPSG:3348")
        self.segment = helpers.explode_geometry(self.segment)
        self.segment = helpers.round_coordinates(self.segment, precision=7)

        # Set identifiers as index and drop unneeded columns.
        self.segment.index = self.segment[self.id]
        self.segment = self.segment[["structure_type", "geometry"]].copy(deep=True)

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
        self.segment["pt_start"] = self.segment["pts_tuple"].map(itemgetter(0))
        self.segment["pt_end"] = self.segment["pts_tuple"].map(itemgetter(-1))
        self.segment["pts_ordered_pairs"] = self.segment["pts_tuple"].map(ordered_pairs)

        # Store computationally intensive lookups.
        pts = self.segment["pts_tuple"].explode()
        pts_df = pd.DataFrame({"pt": pts.values, self.id: pts.index})
        self.pts_id_lookup = helpers.groupby_to_list(pts_df, "pt", self.id).map(set).to_dict()
        self.idx_id_lookup = dict(zip(range(len(self.segment)), self.segment.index))

    def connectivity_min_distance(self) -> dict:
        """
        Validation: Arcs must be >= 5 meters from each other, excluding connected arcs (i.e. no dangles).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Compile all nodes as a DataFrame.
        pts = pd.DataFrame({"pt": self.segment["pt_start"].append(self.segment["pt_end"]).unique()})

        # Generate simplified node buffers with distance tolerance.
        dist = 5
        pts["buffer"] = pts["pt"].map(lambda pt: Point(pt).buffer(dist, resolution=5))

        # Query segments where their bbox intersects each node buffer.
        pts["intersects_idx"] = pts["buffer"].map(lambda buffer: set(self.segment.sindex.query(buffer, "intersects")))

        # Filter to those nodes with multiple intersections.
        pts = pts.loc[pts["intersects_idx"].map(len) > 1]
        if len(pts):

            # Compile identifiers for each intersecting segment index.
            pts["intersects_ids"] = pts["intersects_idx"].map(
                lambda idxs: set(map(lambda idx: itemgetter(idx)(self.idx_id_lookup), idxs)))

            # Compile identifiers for each segment connected to the node.
            pts["node_ids"] = pts["pt"].map(lambda pt: itemgetter(pt)(self.pts_id_lookup))

            # Filter intersecting segment ids to those not containing the node (disconnected segments).
            pts["disconnected_ids"] = pts["intersects_ids"] - pts["node_ids"]

            # Filter to those nodes with multiple disconnected segments.
            pts = pts.loc[pts["disconnected_ids"].map(len) > 0]
            if len(pts):

                # Compile error logs.
                errors["values"] = pts[["disconnected_ids", "node_ids"]].apply(
                    lambda row: f"Disconnected feature(s) are too close: {*row[0],} - {*row[1],}", axis=1).to_list()
                vals = pts["node_ids"].append(pts["disconnected_ids"]).map(tuple).explode().unique()
                errors["query"] = f"\"{self.id}\" in {*vals,}"

        return errors

    def connectivity_node_intersection(self) -> dict:
        """
        Validates: Arcs must only connect at endpoints (nodes).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Compile nodes.
        nodes = set(self.segment["pt_start"].append(self.segment["pt_end"]))

        # Compile interior vertices (non-nodes).
        # Note: only segments with > 2 vertices are used.
        non_nodes = set(self.segment.loc[self.segment["pts_tuple"].map(len) > 2, "pts_tuple"]
                        .map(lambda pts: set(pts[1:-1])).map(tuple).explode())

        # Compile invalid vertices.
        invalid_pts = nodes.intersection(non_nodes)
        if len(invalid_pts):

            # Filter segments to those with an invalid vertex.
            invalid_ids = set(chain.from_iterable(map(lambda pt: itemgetter(pt)(self.pts_id_lookup), invalid_pts)))
            segment = self.segment.loc[self.segment.index.isin(invalid_ids)]

            # Flag invalid segments where the invalid vertex is a non-node.
            flag = segment["pts_tuple"].map(lambda pts: len(set(pts[1:-1]).intersection(invalid_pts))) > 0
            if sum(flag):

                # Compile error logs.
                vals = set(segment.loc[flag].index)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in {*vals,}"

        return errors

    def connectivity_segmentation(self) -> dict:
        """
        Validates: Arcs must not cross (i.e. must be segmented at each intersection).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Query segments which cross each segment.
        crosses = self.segment["geometry"].map(lambda g: set(self.segment.sindex.query(g, predicate="crosses")))

        # Flag segments which have one or more crossing segments.
        flag = crosses.map(len) > 0
        if sum(flag):

            # Segment original geometries.

            # Modify flag to accommodate full (non-filtered) records present in original dataframe.
            roads_flag = self.segment_original.segment_type.astype(int) == 1
            self.segment_original.loc[roads_flag, "index"] = range(sum(roads_flag))
            self.segment_original.loc[~roads_flag, "index"] = range(-1, (sum(~roads_flag) + 1) * -1)
            self.segment_original.index = self.segment_original["index"]
            # TODO: fix.

            # Compile splitter geometries.
            self.segment_original.loc[flag, "splitter"] = crosses.values
            self.segment_original.loc[flag, "splitter"] = self.segment_original.loc[flag, "splitter"]\
                .map(lambda indexes: itemgetter(*indexes)(self.segment_original["geometry"]))\
                .map(lambda geoms: geoms if isinstance(geoms, tuple) else (geoms,))

            # Add original geometry to beginning of splitter tuple.
            self.segment_original.loc[flag, "splitter"] = self.segment_original.loc[flag, ["geometry", "splitter"]]\
                .apply(lambda row: (row[0], *row[1]), axis=1)

            # Iteratively split geometries using each splitter geometry.
            self.segment_original.loc[flag, "geometry"] = self.segment_original.loc[flag, "splitter"].map(
                lambda geoms: reduce(split_line, geoms))

            # Cleanup and standardize source dataframe.
            # 1) Explode multi-part geometries (MultiLineStrings).
            # 2) rounds coordinates to 7 decimal places and flattens to 2-dimensions.
            # 3) Drop temporary attributes.
            self.segment_original = helpers.explode_geometry(self.segment_original)
            self.segment_original = helpers.round_coordinates(self.segment_original, precision=7)
            self.segment_original.drop(columns=["index", "splitter"], inplace=True)

            # Export original dataframe.
            helpers.export(self.segment_original, dst=self.dst, name=self.layer)

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
            if sum(flag):

                # Compile error logs.
                vals = set(coord_pairs.loc[flag].index)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in {*vals,}"

        return errors

    def construction_min_length(self) -> dict:
        """
        Validates: Arcs must be >= 3 meters in length, except structures (e.g. Bridges).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Flag arcs which are too short.
        min_length = 3
        flag = self.segment.length < min_length
        if sum(flag):
            
            # Flag isolated structures (structures not connected to another structure).
            
            # Compile structures.
            structures = self.segment.loc[~self.segment["structure_type"].isin({"Unknown", "None"})]
            
            # Compile duplicated structure nodes.
            structure_nodes = pd.Series(structures["pt_start"].append(structures["pt_end"]))
            structure_nodes_dups = set(structure_nodes.loc[structure_nodes.duplicated(keep=False)])
            
            # Flag isolated structures.
            isolated_structure_index = set(structures.loc[~((structures["pt_start"].isin(structure_nodes_dups)) |
                                                            (structures["pt_end"].isin(structure_nodes_dups)))].index)
            isolated_structure_flag = self.segment.index.isin(isolated_structure_index)
            
            # Modify flag to exclude isolated structures.
            flag = (flag & (~isolated_structure_flag))
            if sum(flag):

                # Compile error logs.
                vals = set(self.segment.loc[flag].index)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in {*vals,}"

        return errors

    def construction_simple(self) -> dict:
        """
        Validates: Arcs must be simple (i.e. must not self-overlap, self-cross, nor touch their interior).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Flag complex (non-simple) geometries.
        flag = ~self.segment.is_simple
        if sum(flag):

            # Compile error logs.
            vals = set(self.segment.loc[flag].index)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in {*vals,}"

        return errors

    def construction_singlepart(self) -> dict:
        """
        Validates: Arcs must be single part (i.e. 'LineString').

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Flag non-LineStrings.
        flag = self.segment.geom_type != "LineString"
        if sum(flag):

            # Compile error logs.
            vals = set(self.segment.loc[flag].index)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in {*vals,}"

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
                vals = set(dups.index)
                errors["values"] = vals
                errors["query"] = f"\"{self.id}\" in {*vals,}"

        return errors

    def duplication_overlap(self) -> dict:
        """
        Validates: Arcs must not overlap (i.e. contain duplicated adjacent vertices).

        :return dict: dict containing error messages and, optionally, a query to identify erroneous records.
        """

        errors = {"values": list(), "query": None}

        # Query segments which overlap each segment.
        overlaps = self.segment["geometry"].map(lambda g: set(self.segment.sindex.query(g, predicate="overlaps")))

        # Flag segments which have one or more overlapping segments.
        flag = overlaps.map(len) > 0

        # Compile error logs.
        if sum(flag):
            vals = set(overlaps.loc[flag].index)
            errors["values"] = vals
            errors["query"] = f"\"{self.id}\" in {*vals,}"

        return errors

    def execute(self) -> None:
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

        except (KeyError, SyntaxError, ValueError) as e:
            logger.exception("Unable to apply validation.")
            logger.exception(e)
            sys.exit(1)
