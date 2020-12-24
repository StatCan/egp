import click
import fiona
import geopandas as gpd
import logging
import os
import pandas as pd
import sqlite3
import sys
from bisect import bisect, bisect_left
from collections import Counter
from itertools import chain
from operator import attrgetter, itemgetter
from osgeo import ogr, osr
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge
from tqdm import tqdm

sys.path.insert(1, os.path.join(sys.path[0], "../../../"))
import helpers


# Set logger.
logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)


class LRS:
    """Class to convert Yukon data from Linear Reference System (LRS) to GeoPackage."""

    def __init__(self, src, dst):
        self.nrn_datasets = dict()
        self.src_datasets = dict()
        self.base_dataset = "tdylrs_centerline_sequence"
        self.geometry_dataset = "tdylrs_centerline"
        self.event_measurement_fields = {"from": "fromkm", "to": "tokm"}
        self.calibrations = {
            "dataset": "tdylrs_calibration_point",
            "id_field": "routeid",
            "measurement_field": "measure",
            "ids": ["004097", "004307", "004349"]
        }
        self.schema = {
            "br_bridge_ln": {
                "fields": ["routeid", "fromdate", "todate", "fromkm", "tokm", "bridge_name"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999')"
            },
            "sm_structure": {
                "fields": ["routeid", "fromdate", "todate", "fromkm", "tokm", "surface_code"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999')"
            },
            "tdylrs_calibration_point": {
                "fields": ["routeid", "fromdate", "todate", "networkid", "measure", "geometry"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999') & networkid==1"
            },
            "tdylrs_centerline": {
                "fields": ["centerlineid", "geometry"],
                "query": None
            },
            "tdylrs_centerline_sequence": {
                "fields": ["centerlineid", "fromdate", "todate", "networkid", "routeid", "centerlineid"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999') & networkid==1"
            },
            "tdylrs_primary_rte": {
                "fields": ["fromdate", "todate", "routeid", "planimetric_accuracy", "acquisition_technique_dv",
                           "acquired_by_dv", "acquisition_date"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999')"
            },
            "td_lane_configuration": {
                "fields": ["routeid", "fromdate", "todate", "fromkm", "tokm", "lane_configuration"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999')"
            },
            "td_number_of_lanes": {
                "fields": ["routeid", "fromdate", "todate", "fromkm", "tokm", "number_of_lanes"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999')"
            },
            "td_road_administration": {
                "fields": ["routeid", "fromdate", "todate", "fromkm", "tokm", "administration"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999')"
            },
            "td_road_type": {
                "fields": ["routeid", "fromdate", "todate", "fromkm", "tokm", "road_type"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999')"
            },
            "td_street_name": {
                "fields": ["routeid", "fromdate", "todate", "fromkm", "tokm", "street_direction_prefix",
                           "street_type_prefix", "street_name", "street_type_suffix", "street_direction_suffix"],
                "query": "todate.isna() & ~fromdate.astype('str').str.startswith('9999')"
            }
        }
        self.structure = {
            "base": self.base_dataset,
            "connections": {
                "centerlineid": ["tdylrs_centerline"],
                "routeid": ["br_bridge_ln", "sm_structure", "tdylrs_calibration_point", "tdylrs_primary_rte",
                            "td_lane_configuration", "td_number_of_lanes", "td_road_administration", "td_road_type",
                            "td_street_name"]
            }
        }

        # Validate src.
        self.src = os.path.abspath(src)
        if os.path.splitext(self.src)[-1] != ".gdb":
            logger.exception(f"Invalid src input: {src}. Must be a File GeoDatabase.")
            sys.exit(1)

        # Validate dst.
        self.dst = os.path.abspath(dst)
        if os.path.splitext(self.dst)[-1] != ".gpkg":
            logger.exception(f"Invalid dst input: {dst}. Must be a GeoPackage.")
            sys.exit(1)
        if os.path.exists(self.dst):
            logger.exception(f"Invalid dst input: {dst}. File already exists.")

    def assemble_segmented_network(self):
        """Assembles a segmented network from the distributed datasets."""

        calibrations_df = self.src_datasets[self.calibrations["dataset"]]

        def segment_geometry(breakpts, geom):
            """
            Returns a (Multi)LineString, representing the original geometry segmented at the given breakpoints.
            To increase splitting accuracy, breakpoints will be snapped to pre-existing nodes in the geometry, where
            possible.
            """

            # Return entire geometry if breakpoints cover entire length.
            if breakpts[0] == 0 and round(breakpts[-1]) == round(geom.length):
                return [geom]

            # Linestring.
            elif isinstance(geom, LineString):

                # Get LineString nodes and distances.
                nodes = list(geom.coords)
                nodes_dist = list(map(lambda node: geom.project(Point(node)), nodes))

                # Compile all nodes between breakpoints.
                rng = [bisect_left(nodes_dist, breakpts[0]), bisect(nodes_dist, breakpts[-1])]
                nodes_keep = nodes[rng[0]: rng[-1]]

                # Conditionally create start and end nodes if the breakpoints don't match any pre-existing node.
                if round(breakpts[0]) < round(nodes_dist[rng[0]]):
                    nodes_keep = [geom.interpolate(breakpts[0]), *nodes_keep]
                if round(breakpts[-1]) > round(nodes_dist[rng[-1] - 1]):
                    nodes_keep = [*nodes_keep, geom.interpolate(breakpts[-1])]

                return LineString(nodes_keep)

            # MultiLineString.
            else:

                geoms = list()

                # Compile individual LineString lengths, relative to the full MultiLineString.
                lengths = [line.length for line in geom]
                lengths_rng = [pd.Interval(sum(lengths[:i]), sum(lengths[:i+1])) for i in range(len(lengths))]
                lengths = [sum(lengths[:i+1]) for i in range(len(lengths))]

                # Add intermediary lengths (LineString transitions) to breakpoints.
                breakpts = [breakpts[0], *[l for l in lengths if breakpts[0] < l < breakpts[-1]], breakpts[-1]]

                # Iterate breakpoint pairs and segment corresponding LineString.
                for index in range(len(breakpts)-1):
                    breakpts_ = breakpts[index: index+2]
                    geom_idx = [idx for idx, rng in enumerate(lengths_rng) if pd.Interval(*breakpts_).overlaps(rng)][0]
                    geom_ = geom[geom_idx]

                    geoms.append(segment_geometry(breakpts_, geom_))

                return MultiLineString(geoms)

        def sort_multilinestring(con_id, geom):
            """Sorts a MultiLineString into the correct LineString ordering based on calibration points."""

            # Compile sorted calibration points for connection ID.
            calibration_pts = calibrations_df.loc[calibrations_df[self.calibrations["id_field"]] == con_id] \
                .sort_values(self.calibrations["measurement_field"])

            # Get LineString index order by intersecting calibration points with LineStrings.
            index_order = list(dict.fromkeys(chain.from_iterable(calibration_pts["geometry"].map(
                lambda pt: [index for index, line in enumerate(geom) if pt.intersects(line)]).to_list())))

            # Add missing indexes.
            # Note: indexes will not be missing with topologically correct geometries, however, these errors have been
            # identified in the data and it is preferred to accommodate them here and flag them collectively in the
            # actual NRN pipeline.
            missing = set(range(len(geom))) - set(index_order)
            index_order.extend(missing)

            # Create MultiLineString from LineString index ordering.
            return MultiLineString(itemgetter(*index_order)(geom))

        logger.info("Assembling segmented network.")

        # Assemble base - geometry connection.
        logger.info(f"Assembling base - geometry connection: {self.base_dataset} - {self.geometry_dataset}.")

        # Assemble datasets.
        base = gpd.GeoDataFrame(self.src_datasets[self.base_dataset].merge(
            self.src_datasets[self.geometry_dataset], how="left", on=self.get_con_id_field(self.geometry_dataset)))

        # Explode geometries to singlepart.
        base = helpers.explode_geometry(base)

        # Merge geometries for many-to-one links; keep only the first record but keep the entire merged geometry.
        con_id_field = self.calibrations["id_field"]
        flag = base[con_id_field].duplicated(keep=False)
        geom_links = dict(helpers.groupby_to_list(base.loc[flag], con_id_field, "geometry").map(linemerge))
        base = base.loc[~base[con_id_field].duplicated(keep="first")]
        base.loc[flag, "geometry"] = base.loc[flag, con_id_field].map(geom_links)

        # Sort MultiLineStrings into proper LineString ordering.
        logger.info(f"Sorting MultiLineStrings into proper LineString ordering.")

        con_id_field = self.calibrations["id_field"]
        flag = base.geom_type == "MultiLineString"
        base.loc[flag, "geometry"] = base.loc[flag, [con_id_field, "geometry"]].apply(
            lambda row: sort_multilinestring(*row), axis=1)

        # Iterate datasets and assemble all event measurements for each base geometry.
        logger.info(f"Compiling all event measurements as breakpoints.")

        for name, df in self.src_datasets.items():
            if name not in {self.base_dataset, self.geometry_dataset} and {"from", "to"}.issubset(set(df.columns)):
                logger.info(f"Compiling breakpoints for dataset: {name}.")

                # Identify connection field.
                con_id_field = self.get_con_id_field(name)

                # Compile breakpoints as flattened list.
                df["breakpts"] = df[["from", "to"]].apply(lambda row: [*row], axis=1)
                breakpts = helpers.groupby_to_list(df, con_id_field, "breakpts").map(chain.from_iterable).map(list)

                # Merge breakpoints with base dataset.
                breakpts.name = f"{name}_breakpts"
                base = base.merge(breakpts, how="left", left_on=con_id_field, right_index=True)

        # Reduce and sort breakpoints into flattened lists.
        logger.info(f"Reducing and sorting breakpoints.")

        breakpt_cols = [col for col in base.columns if col.endswith("_breakpts")]
        base["breakpts"] = base[breakpt_cols].apply(
            lambda row: chain.from_iterable(r for r in row if isinstance(r, list)), axis=1).map(set).map(sorted)

        # Remove extraneous columns.
        base.drop(columns=breakpt_cols, inplace=True)

        # Add geometry start and end breakpoints.
        # Note: remove breakpoints which are within 1 unit distance from the start and end breakpoints.
        logger.info(f"Adding geometry start and end to breakpoints.")

        base["breakpts"] = base[["breakpts", "geometry"]].apply(
            lambda row: [0, *[pt for pt in breakpts if 1 <= pt <= (row[1].length-1)], row[1].length], axis=1)

        # Filter breakpoints which are too close together.
        logger.info(f"Filtering breakpoints which are too close together.")

        # Filter breakpoints by keeping only those which are more than 1 unit distance from the next breakpoint.
        base["breakpts"] = base["breakpts"].map(
            lambda pts: [*[pt for index, pt in enumerate(pts[:-1])
                           if abs(round(pt) - round(pts[index+1])) > 1], breakpts[-1]])

        # Split record geometries on breakpoints.
        logger.info(f"Splitting records on geometry breakpoints.")

        # Nest breakpoints into groups of 2.
        base["breakpts"] = base["breakpts"].map(lambda pts: [[pts[i], pts[i+1]] for i in range(len(pts)-1)])

        # Explode dataframe on breakpoints.
        # Note: must use pandas dataframe since geodataframe.explode is geometry based.
        base = gpd.GeoDataFrame(pd.DataFrame(base).explode("breakpts", ignore_index=True))

        # Extract geometry segment corresponding to breakpoints.
        # Note: for unique connection IDs, keep the entire geometry.
        self.base = base
        base["geometry"] = base[["breakpts", "geometry"]].apply(lambda row: segment_geometry(*row), axis=1)

        # Assemble matching attributes.
        # Assemble datasets without segmentation....tdylrs_primary_rte.

    def clean_event_measurements(self):
        """
        Performs several cleanup operations on records based on event measurement:
        1. Simplifies event measurement field names to 'from' and 'to'.
        2. Converts measurements to crs unit (current conversion = km to m).
        3. Drops records with invalid measurements (from >= to).
        4. Matches event measurements to any corresponding calibration point measurements (for improved accuracy).
        5. Removes event measurement offsets for out-of-scope records: some records do not start at zero because they
        begin outside of the territory. The event measurements on these records must be reduced according to the
        starting offset.
        6. Repairs gaps in event measurements along the same connected feature.
        7. Flags overlapping event measurements along the same connected feature.
        """

        def match_calibration_pts(con_id, event):
            """Swaps an event measurement for a corresponding calibration point measurements, if possible."""

            # Filter calibration point to connection ID.
            measurements = calibrations_df.loc[calibrations_df[self.calibrations["id_field"]] == con_id,
                                               self.calibrations["measurement_field"]]

            # Identify matching calibration points for event (tolerance = 1 unit).
            matching_measurements = measurements.loc[measurements.subtract(event).abs() <= 1]
            if len(matching_measurements):
                return matching_measurements.iloc[0]
            else:
                return event

        logger.info("Cleaning event measurement fields.")
        fields = self.event_measurement_fields

        # Compile offsets for event measurements.
        offsets = dict()
        id_field, offset_field = itemgetter("id_field", "measurement_field")(self.calibrations)

        # Convert calibration point measurement units identically to event measurements.
        self.src_datasets[self.calibrations["dataset"]][offset_field] = self.src_datasets[
            self.calibrations["dataset"]][offset_field].multiply(1000)

        # Compile offsets for out-of-scope events.
        calibrations_df = self.src_datasets[self.calibrations["dataset"]]
        calibrations_df = calibrations_df.loc[calibrations_df[id_field].isin(self.calibrations["ids"])]
        for offset_id in set(calibrations_df[id_field]):
            offsets[offset_id] = calibrations_df.loc[calibrations_df[id_field] == offset_id, offset_field].min()

        # Iterate dataframes with event measurement fields.
        for layer, df in self.src_datasets.items():
            if set(fields.values()).issubset(df.columns):

                logger.info(f"Cleaning event measurements for dataset: {layer}.")

                # Identify connection field.
                con_id_field = self.get_con_id_field(layer)

                # Convert measurements.
                logger.info("Converting event measurements.")

                df[list(fields.values())] = df[fields.values()].multiply(1000)
                df.rename(columns={fields["from"]: "from", fields["to"]: "to"}, inplace=True)

                # Remove records with invalid event measurements.
                logger.info("Removing records with invalid event measurements.")

                count = len(df)
                df = df.loc[df["from"] < df["to"]].copy(deep=True)
                logger.info(f"Dropped {count - len(df)} of {count} records.")

                # Match event measurements to calibration points, if possible.
                logger.info(f"Matching event measurements against calibration points.")

                count = 0
                for fld in {"from", "to"}:
                    orig = df[fld]

                    flag = df[fld] != 0
                    df.loc[flag, fld] = df.loc[flag, [con_id_field, fld]].apply(
                        lambda row: match_calibration_pts(*row), axis=1)

                    count += (orig != df[fld]).sum()

                logger.info(f"Matched {count} event measurements to calibration points.")

                # Update out-of-scope offsets.
                logger.info("Updating out-of-scope offsets for events measurements.")

                for offset_id, offset in offsets.items():
                    flag = df[con_id_field] == offset_id
                    df.loc[flag, ["from", "to"]] = df.loc[flag, ["from", "to"]].subtract(offset)

                    logger.info(f"Updated {flag.sum()} offset event measurements for {con_id_field}={offset_id}.")

                # Repair gaps in measurement ranges.
                logger.info("Repairing event measurement gaps.")

                # Iterate records with duplicated connection ids.
                update_count = 0
                dup_con_ids = set(df.loc[df[con_id_field].duplicated(keep=False), con_id_field])
                for con_id in dup_con_ids:
                    records = df.loc[df[con_id_field] == con_id]
                    from_min = records["from"].min()

                    # For any gaps (tolerance = 1 unit), reduce the 'from' measurement to the appropriate neighbouring
                    # 'to' measurement.
                    for index, from_value in records.loc[records["from"] != from_min, "from"].iteritems():
                        neighbour = records[(records.index != index) & ((from_value - records["to"]).between(0, 1))]
                        if len(neighbour):

                            # Update record.
                            df.loc[index, "from"] = neighbour["to"].iloc[0]
                            update_count += 1

                logger.info(f"Repaired {update_count} event measurement gaps.")

                # Flag overlapping measurement ranges.
                logger.info("Identifying overlapping event measurement ranges.")

                # Iterate records with duplicated connection ids.
                for con_id in dup_con_ids:
                    overlap_flag = False

                    # Create intervals from event measurements.
                    intervals = df.loc[df[con_id_field] == con_id, ["from", "to"]].apply(
                        lambda row: pd.Interval(*row), axis=1).to_list()

                    # Flag connection id if overlapping intervals are detected.
                    for idx, i1 in enumerate(intervals):
                        for i2 in intervals[idx + 1:]:
                            if i1.overlaps(i2):
                                overlap_flag = True
                                break
                        if overlap_flag:
                            break

                    if overlap_flag:
                        logger.warning(f"Overlap detected: {con_id_field}={con_id}.")

                # Store results.
                self.src_datasets[layer] = df.copy(deep=True)

    def compile_source_datasets(self):
        """Loads source layers into (Geo)DataFrames."""

        logger.info(f"Compiling source datasets from: {self.src}.")

        rename = {
            "acquired_by_dv": "provider",
            "acquisition_date": "credate",
            "acquisition_technique_dv": "acqtech",
            "administration": "roadjuris",
            "bridge_name": "strunameen",
            "fromdate": "revdate",
            "lane_configuration": "trafficdir",
            "number_of_lanes": "nbrlanes",
            "planimetric_accuracy": "accuracy",
            "road_type": "roadclass",
            "street_direction_prefix": "dirprefix",
            "street_direction_suffix": "dirsuffix",
            "street_name": "namebody",
            "street_type_prefix": "strtypre",
            "street_type_suffix": "strtysuf",
            "surface_code": "pavstatus"
        }

        # Compile layer names for lowercase lookup.
        layers_lower = {name.lower(): name for name in fiona.listlayers(self.src)}

        # Iterate LRS schema.
        for index, items in enumerate(self.schema.items()):

            layer, attr = itemgetter(0, 1)(items)

            logger.info(f"Compiling source dataset {index + 1} of {len(self.schema)}: {layer}.")

            # Load layer into dataframe, force lowercase column names.
            df = gpd.read_file(self.src, driver="OpenFileGDB", layer=layers_lower[layer]).rename(columns=str.lower)

            # Filter columns.
            df.drop(columns=df.columns.difference(attr["fields"]), inplace=True)

            # Filter records with query.
            if attr["query"]:
                count = len(df)
                df.query(attr["query"], inplace=True)
                logger.info(f"Dropped {count - len(df)} of {count} records for dataset: {layer}, based on query.")

            # Update column names to match NRN.
            df.rename(columns=rename, inplace=True)

            # Convert tabular dataframes.
            if "geometry" not in df.columns:
                df = pd.DataFrame(df)

            # Store results.
            self.src_datasets[layer] = df.copy(deep=True)

    def configure_valid_records(self):
        """
        Filters records to only those which link to the base dataset.
        Flags many-to-one linkages between the base and geometry datasets.
        """

        logger.info(f"Configuring valid records.")

        # Iterate dataframes and remove records which do not link to the base dataset.
        for name, df in {k: v for k, v in self.src_datasets.items() if k != self.base_dataset}.items():

            logger.info(f"Configuring valid records for source dataset: {name}.")

            # Identify connection field.
            con_id_field = self.get_con_id_field(name)

            # Compile valid IDs from base dataset for the identified connection field.
            valid_ids = set(self.src_datasets[self.base_dataset][con_id_field])

            # Remove records with invalid connection IDs.
            df_valid = df.loc[df[con_id_field].isin(valid_ids)]
            logger.info(f"Dropped {len(df) - len(df_valid)} of {len(df)} records for dataset: {name}, based on ID "
                        f"field: {con_id_field}.")

            # Flag many-to-one linkages between base and geometry datasets.
            if name == self.geometry_dataset:

                # Compile and flag many-to-one linkages.
                base = self.src_datasets[self.base_dataset]
                for con_id, count in Counter(base.loc[base[con_id_field].duplicated(keep=False), con_id_field]).items():
                    logger.warning(f"Many-to-one linkage identified between base ({self.base_dataset}) and geometry "
                                   f"({self.geometry_dataset}) datasets: {con_id_field}={con_id}, count={count}.")

            # Store or remove dataset.
            if len(df_valid):
                self.src_datasets[name] = df_valid.copy(deep=True)
            else:
                del self.src_datasets[name]

    def export_gpkg(self):
        """Exports the NRN datasets to a GeoPackage."""

        logger.info(f"Exporting datasets to GeoPackage: {self.dst}.")

        try:

            logger.info(f"Creating data source: {self.dst}.")

            # Create GeoPackage.
            driver = ogr.GetDriverByName("GPKG")
            gpkg = driver.CreateDataSource(self.dst)

            # Iterate dataframes.
            for name, df in self.nrn_datasets.items():

                logger.info(f"Layer {name}: creating layer.")

                # Configure layer shape type and spatial reference.
                if isinstance(df, gpd.GeoDataFrame):

                    srs = osr.SpatialReference()
                    srs.ImportFromEPSG(df.crs.to_epsg())

                    if len(df.geom_type.unique()) > 1:
                        raise ValueError(f"Multiple geometry types detected for dataframe {name}: "
                                         f"{', '.join(map(str, df.geom_type.unique()))}.")
                    elif df.geom_type[0] in {"Point", "MultiPoint", "LineString", "MultiLineString"}:
                        shape_type = attrgetter(f"wkb{df.geom_type[0]}")(ogr)
                    else:
                        raise ValueError(f"Invalid geometry type(s) for dataframe {name}: "
                                         f"{', '.join(map(str, df.geom_type.unique()))}.")
                else:
                    shape_type = ogr.wkbNone
                    srs = None

                # Create layer.
                layer = gpkg.CreateLayer(name=name, srs=srs, geom_type=shape_type, options=["OVERWRITE=YES"])

                logger.info(f"Layer {name}: configuring schema.")

                # Configure layer schema (field definitions).
                ogr_field_map = {"f": ogr.OFTReal, "i": ogr.OFTInteger, "O": ogr.OFTString}

                for field_name, dtype in df.dtypes.items():
                    if field_name != "geometry":
                        field_defn = ogr.FieldDefn(field_name, ogr_field_map[dtype.kind])
                        layer.CreateField(field_defn)

                # Write layer.
                layer.StartTransaction()

                for feat in tqdm(df.itertuples(index=False), total=len(df), desc=f"Layer {name}: writing to file"):

                    # Instantiate feature.
                    feature = ogr.Feature(layer.GetLayerDefn())

                    # Set feature properties.
                    properties = feat._asdict()
                    for prop in set(properties) - {"geometry"}:
                        field_index = feature.GetFieldIndex(prop)
                        feature.SetField(field_index, properties[prop])

                    # Set feature geometry, if required.
                    if srs:
                        geom = ogr.CreateGeometryFromWkb(properties["geometry"].wkb)
                        feature.SetGeometry(geom)

                    # Create feature.
                    layer.CreateFeature(feature)

                    # Clear pointer for next iteration.
                    feature = None

                layer.CommitTransaction()

        except (Exception, KeyError, ValueError, sqlite3.Error) as e:
            logger.exception(f"Error raised when writing to GeoPackage: {self.dst}.")
            logger.exception(e)
            sys.exit(1)

    def get_con_id_field(self, name):
        """Returns the connection ID field, relative to the base dataset, for the given dataset name."""

        for con_field, df_names in self.structure["connections"].items():
            if name in df_names:
                return con_field

    def execute(self):
        """Executes class functionality."""

        self.compile_source_datasets()
        self.configure_valid_records()
        self.clean_event_measurements()
        self.assemble_segmented_network()
        # self.export_gpkg()


@click.command()
@click.argument("src", type=click.Path(exists=True))
@click.option("--dst", type=click.Path(exists=False), default=os.path.abspath("../../../../data/raw/yt/yt.gpkg"),
              show_default=True)
def main(src, dst):
    """Executes the LRS class."""

    try:

        with helpers.Timer():
            lrs = LRS(src, dst)
            lrs.execute()

    except KeyboardInterrupt:
        logger.exception("KeyboardInterrupt: Exiting program.")
        sys.exit(1)


if __name__ == "__main__":
    main()
