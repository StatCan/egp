import click
import fiona
import geopandas as gpd
import logging
import os
import pandas as pd
import sqlite3
import sys
import uuid
import yaml
from numpy import nan
from shutil import copy

sys.path.insert(1, os.path.join(sys.path[0], ".."))
import field_map_functions
import helpers


# Set logger.
logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)


class Stage:
    """Defines an NRN stage."""

    def __init__(self, source):
        self.stage = 1
        self.source = source.lower()

        # Configure raw data path.
        self.data_path = os.path.abspath("../../data/raw/{}".format(self.source))

        # Configure source attribute path.
        self.source_attribute_path = os.path.abspath("sources/{}".format(self.source))

        # Create temp dir.
        self.temp_dir = helpers.create_temp_directory(self.stage)

    def apply_domains(self):
        """Applies the field domains to each column in the target (geo)dataframes."""

        logging.info("Applying field domains.")

        # Retrieve field domains.

    def apply_field_mapping(self):
        """Maps the source geodataframes to the target geodataframes via user-specific field mapping functions."""

        logger.info("Applying field mapping.")

        # Retrieve source attributes and geodataframe.
        for source_name, source_attributes in self.source_attributes.items():
            source_gdf = self.source_gdframes[source_name]

            # Retrieve target attributes.
            for target_name in source_attributes["conform"]:
                logger.info("Applying field mapping from {} to {}.".format(source_name, target_name))

                # Retrieve table field mapping attributes.
                map_attributes = source_attributes["conform"][target_name]

                # Field mapping.
                for target_field, source_field in map_attributes.items():

                    # Retrieve target geodataframe.
                    target_gdf = self.target_gdframes[target_name]

                    # No mapping.
                    if source_field is None:
                        logger.info("Target field \"{}\": No mapping provided.".format(target_field))

                    # Raw value mapping.
                    elif isinstance(source_field, str) and (source_field not in source_gdf.columns):
                            logger.info("Target field \"{}\": Applying raw value field mapping.".format(target_field))

                            # Update target geodataframe with raw value.
                            target_gdf[target_field] = source_field

                    # # Field mapping type: split_record.
                    # elif "split_record" in source_field["functions"].keys():
                    #     logger.info("Target field \"{}\": Applying split_record field mapping.".format(target_field))

                    # Function mapping.
                    else:
                        logger.info("Target field \"{}\": Identifying function chain.".format(target_field))

                        # Reconfigure direct field mapping in case of string input.
                        if isinstance(source_field, str):
                                source_field = {"fields": [source_field], "functions": {"direct": {"param": None}}}

                        # Convert field attribute to list.
                        if isinstance(source_field["fields"], str):
                            source_field["fields"] = [source_field["fields"]]

                        # Create mapped dataframe from source and target geodataframes, keeping only source fields.
                        # Convert to series.
                        mapped_series = pd.DataFrame({field: target_gdf["uuid"].map(source_gdf.set_index("uuid")[field])
                                                      for field in source_field["fields"]})
                        mapped_series = mapped_series.apply(lambda row: row[0] if len(row) == 1 else row.values, axis=1)

                        # Apply field mapping functions to mapped dataframe.
                        # Update target geodataframe.
                        target_gdf[target_field] = self.apply_functions(map_attributes, mapped_series,
                                                                        source_field["functions"])

                    # Store updated target geodataframe.
                    self.target_gdframes[target_name] = target_gdf

    def apply_functions(self, map_attributes, series, func_dict):
        """Iterates and applies field mapping function(s) to a Pandas series."""

        # Iterate functions.
        for func, params in func_dict.items():

            logger.info("Applying field mapping function: {}.".format(func))

            # Advanced function mapping - copy_attribute_functions.
            if func == "copy_attribute_functions":

                # Retrieve and iterate attribute functions and parameters.
                for attribute_func_dict in field_map_functions.copy_attribute_functions(map_attributes, params):
                    series = self.apply_functions(map_attributes, series, attribute_func_dict)

            else:
                # Apply function.
                series = series.apply(lambda row: eval("field_map_functions.{}({}, **{})".format(
                    func, self.format_row(row), params)))

        return series

    def export_gpkg(self):
        """Exports the target (Geo)DataFrames as GeoPackage layers."""

        logger.info("Exporting target geodataframes to GeoPackage layers.")

        # Configure GeoPackage path.
        gpkg_path = os.path.join(self.temp_dir, "{}.gpkg".format(self.source))

        # TEST
        print(self.source_gdframes["geonb_nbrn-rrnb_road-route"].columns, "\n")
        for i in range(0, 3):
            print(self.source_gdframes["geonb_nbrn-rrnb_road-route"].values[i], "\n")
        print(self.target_gdframes["strplaname"].columns, "\n")
        for i in range(0, 3):
            print(self.target_gdframes["strplaname"].values[i], "\n")
        sys.exit()
        # TEST

        # Export target geodataframes to GeoPackage layers.
        try:
            for name, gdf in self.target_gdframes.items():

                logger.info("Writing to GeoPackage {}, layer={}.".format(gpkg_path, name))

                # Spatial data.
                if "geometry" in dir(gdf):
                    # Open GeoPackage.
                    with fiona.open(gpkg_path, "w", layer=name, driver="GPKG", crs=gdf.crs,
                                    schema=gpd.io.file.infer_schema(gdf)) as gpkg:

                        # Write to GeoPackage.
                        gpkg.writerecords(gdf.iterfeatures())

                # Tabular data.
                else:
                    # Copy geopackage template.
                    copy(os.path.abspath("../../data/empty.gpkg"), gpkg_path)

                    # Create sqlite connection.
                    con = sqlite3.connect(gpkg_path)

                    # Write to GeoPackage.
                    gdf.to_sql(name, con)

                    # Insert record into gpkg_contents metadata table.
                    con.cursor().execute("insert into 'gpkg_contents' ('table_name', 'data_type') values "
                                         "('{}', 'attributes');".format(name))

                    # Commit and close db connection.
                    con.commit()
                    con.close()

                logger.info("Successfully exported layer.")

        except (ValueError, fiona.errors.FionaValueError):
            logger.error("ValueError raised when writing GeoPackage layer.")
            sys.exit(1)

    @staticmethod
    def format_row(row):
        """Formats a Pandas Series prior to inclusion in an eval() expression."""

        if isinstance(row, str):
            return "'{}'".format(row.replace("'", "\\'"))
        else:
            return row

    def gen_source_geodataframes(self):
        """Loads input data into a GeoPandas dataframe."""

        logger.info("Loading input data as geodataframes.")
        self.source_gdframes = dict()

        for source, source_yaml in self.source_attributes.items():
            # Configure filename attribute absolute path.
            source_yaml["data"]["filename"] = os.path.join(self.data_path, source_yaml["data"]["filename"])

            # Load source into geodataframe.
            try:
                gdf = gpd.read_file(**source_yaml["data"])
            except fiona.errors.FionaValueError:
                logger.error("ValueError raised when importing source {}, layer={}".format(
                    source_yaml["data"]["filename"], source_yaml["data"]["layer"]))
                sys.exit(1)

            # Force lowercase field names.
            gdf.columns = map(str.lower, gdf.columns)

            # Add uuid field.
            gdf["uuid"] = [uuid.uuid4().hex for _ in range(len(gdf))]

            # Store result.
            self.source_gdframes[source] = gdf
            logger.info("Successfully loaded geodataframe for {}, layer={}.".format(
                os.path.basename(source_yaml["data"]["filename"]), source_yaml["data"]["layer"]))

    def gen_target_geodataframes(self):
        """Creates empty geodataframes for all applicable output tables based on the input data field mapping."""

        logger.info("Creating target geodataframes for applicable tables.")
        self.target_gdframes = dict()

        # Retrieve target table name from source attributes.
        for source, source_yaml in self.source_attributes.items():
            for table in source_yaml["conform"]:

                logger.info("Creating target geodataframe: {}.".format(table))

                # Spatial.
                if self.target_attributes[table]["spatial"]:

                    # Generate target geodataframe from source uuid and geometry fields.
                    gdf = gpd.GeoDataFrame(self.source_gdframes[source][["uuid"]],
                                           geometry=self.source_gdframes[source].geometry)

                # Tabular.
                else:

                    # Generate target dataframe from source uuid field.
                    gdf = pd.DataFrame(self.source_gdframes[source][["uuid"]])

                # Add target field schema.
                gdf = gdf.assign(**{field: pd.Series(dtype=dtype) for field, dtype in
                                    self.target_attributes[table]["fields"].items()})

                # Store result.
                self.target_gdframes[table] = gdf
                logger.info("Successfully created target geodataframe: {}.".format(table))

    def load_source_attributes(self):
        """Loads the yaml files in the sources' directory into a dictionary."""

        logger.info("Identifying source attribute files.")
        files = [os.path.join(self.source_attribute_path, f) for f in os.listdir(self.source_attribute_path) if
                 f.endswith(".yaml")]

        logger.info("Loading source attribute yamls.")
        self.source_attributes = dict()

        for f in files:
            # Load yaml.
            with open(f, "r") as source_attributes_file:
                try:
                    source_attributes_yaml = yaml.safe_load(source_attributes_file)
                except yaml.YAMLError:
                    logger.error("Unable to load yaml file: {}.".format(f))
                    sys.exit(1)

            # Store yaml contents.
            self.source_attributes[os.path.splitext(os.path.basename(f))[0]] = source_attributes_yaml

    def load_target_attributes(self):
        """Loads the target (distribution format) yaml file into a dictionary."""

        logger.info("Loading target attribute yaml.")
        self.target_attributes = dict()
        target_attributes_path = os.path.abspath("../distribution_format.yaml")

        # Load yaml.
        with open(target_attributes_path, "r") as target_attributes_file:
            try:
                target_attributes_yaml = yaml.safe_load(target_attributes_file)
            except yaml.YAMLError:
                logger.error("Unable to load yaml file: {}.".format(target_attributes_path))
                sys.exit(1)

        logger.info("Compiling attributes for target tables.")
        # Store yaml contents for all contained table names.
        for table in target_attributes_yaml:
            self.target_attributes[table] = {"spatial": target_attributes_yaml[table]["spatial"], "fields": dict()}

            for field, vals in target_attributes_yaml[table]["fields"].items():
                # Compile field attributes.
                try:
                    self.target_attributes[table]["fields"][field] = str(vals[0])
                except ValueError:
                    logger.error("Invalid schema definition for table: {}, field: {}.".format(table, field))
                    sys.exit(1)

    def execute(self):
        """Executes an NRN stage."""

        self.load_source_attributes()
        self.load_target_attributes()
        self.gen_source_geodataframes()
        self.gen_target_geodataframes()
        self.apply_field_mapping()
        self.apply_domains()
        self.export_gpkg()


@click.command()
@click.argument("source", type=click.Choice(["ab", "bc", "mb", "nb", "nl", "ns", "nt", "nu", "on", "pe", "qc", "sk",
                                             "yt", "parks_canada"], case_sensitive=False))
def main(source):
    """Executes an NRN stage."""

    logger.info("Started.")

    stage = Stage(source)
    stage.execute()

    logger.info("Finished.")

if __name__ == "__main__":
    try:

        main()

    except KeyboardInterrupt:
        logger.error("KeyboardInterrupt exception: exiting program.")
        sys.exit(1)
