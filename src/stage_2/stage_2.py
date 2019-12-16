import os
import sys
import networkx as nx
import geopandas as gpd
import pandas as pd
import uuid
from datetime import datetime
from sqlalchemy import *
from sqlalchemy.engine.url import URL
from shapely.geometry.multipoint import MultiPoint
from shapely.geometry.point import Point
from geopandas_postgis import PostGIS

sys.path.insert(1, os.path.join(sys.path[0], ".."))
import helpers
import logging

# Set logger.
logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)

# start script timer
startTime = datetime.now()

# postgres connection parameters
host = 'localhost'
db = 'nrn'
user = 'postgres'
port = 5432
pwd = 'password'

# postgres database url
db_url = URL(drivername='postgresql+psycopg2', host=host, database=db, username=user, port=port, password=pwd)
# engine to connect to postgres
engine = create_engine(db_url)


class Stage:

    def __init__(self):

        # Configure and validate input data path.
        self.data_path = os.path.abspath("../../data/interim/nb.gpkg")
        if not os.path.exists(self.data_path):
            logger.exception("Input data not found: \"{}\".".format(self.data_path))
            sys.exit(1)

    def load_gpkg(self):
        """Loads input GeoPackage layers into dataframes."""

        logger.info("Loading Geopackage layers.")

        self.dframes = helpers.load_gpkg(self.data_path)
        # print(self.dframes["roadseg"])

    def gen_dead_end(self):
        """Generates dead end junctions with NetworkX."""

        logging.info("Converting roadseg dataframe to shapefile for NetworkX.")
        self.df_roadseg = gpd.GeoDataFrame(self.dframes["roadseg"], geometry='geometry')
        self.df_roadseg.to_file("../../data/raw/netx.shp", driver="ESRI Shapefile")
        self.df_roadseg.crs = {'init': 'epsg:4617'}

        logging.info("Read generated shapefiles for dead end creation.")
        graph = nx.read_shp("../../data/raw/netx.shp")

        logging.info("Create an empty graph for dead ends junctions.")
        dead_ends = nx.Graph()

        logging.info("Filter for dead end junctions.")
        dead_ends_filter = [node for node, degree in graph.degree() if degree == 1]

        logging.info("Insert filtered dead end junctions into empty graph.")
        dead_ends.add_nodes_from(dead_ends_filter)

        logging.info("Write dead end junctions shapefile.")
        nx.write_shp(dead_ends, "../../data/raw/dead_end.shp")

        logging.info("Create dead end geodataframe.")
        self.dead_end_gdf = gpd.read_file("../../data/raw/dead_end.shp")
        self.dead_end_gdf["junctype"] = "Dead End"
        print(self.dead_end_gdf)

    def gen_intersections(self):
        """Generates intersection junction types."""

        logging.info("Importing roadseg geodataframe into PostGIS.")
        self.df_roadseg.postgis.to_postgis(con=engine, table_name="stage_2", geometry="LineString", if_exists="replace")

        logging.info("Loading SQL yaml.")
        self.sql = helpers.load_yaml("../sql.yaml")

        inter_filter = self.sql["intersections"]["query"]

        logging.info("Executing SQL injection for junction intersections.")
        self.inter_gdf = gpd.GeoDataFrame.from_postgis(inter_filter, engine, geom_col="geometry")
        self.inter_gdf["junctype"] = "Intersection"
        self.inter_gdf.crs = {'init': 'epsg:4617'}
        # inter_df = pd.DataFrame(self.inter_gdf)
        print(self.inter_gdf)

        logging.info("Writing junction intersection GeoPackage.")
        # inter_gdf.to_file("../../data/raw/intersections.gpkg", driver="GPKG")
        # helpers.export_gpkg(inter_gpd, "../../data/raw/intersections2.gpkg")

    def gen_ferry(self):
        """Generates ferry junctions with NetworkX."""

        logging.info("Converting ferryseg dataframe to shapefile for NetworkX.")
        self.df_ferryseg = gpd.GeoDataFrame(self.dframes["ferryseg"], geometry='geometry')
        self.df_ferryseg.to_file("../../data/raw/netx_ferry.shp", driver="ESRI Shapefile")
        self.df_ferryseg.crs = {'init': 'epsg:4617'}

        logging.info("Read generated shapefile for ferry junction creation.")
        graph_ferry = nx.read_shp("../../data/raw/netx_ferry.shp")

        logging.info("Create an empty graph for ferry junctions.")
        ferry_junc = nx.Graph()

        logging.info("Filter for ferry junctions.")
        ferry_filter = [node for node, degree in graph_ferry.degree() if degree > 0]

        logging.info("Insert filtered ferry junctions into empty graph.")
        ferry_junc.add_nodes_from(ferry_filter)

        logging.info("Write ferry junctions shapefile.")
        nx.write_shp(ferry_junc, "../../data/raw/ferry_dead_end.shp")

        logging.info("Create ferry geodataframe.")
        self.ferry_gdf = gpd.read_file("../../data/raw/ferry.shp")
        self.ferry_gdf["junctype"] = "Ferry"
        self.ferry_gdf.crs = {'init': 'epsg:4617'}
        print(self.ferry_gdf)

    def combine(self):
        """Combine geodataframes."""

        logging.info("Combining ferry, dead end and intersection junctions.")
        junctions = gpd.GeoDataFrame(pd.concat([self.ferry_gdf, self.dead_end_gdf, self.inter_gdf], sort=False))
        junctions = junctions[['junctype', 'geometry']]
        junctions.crs = {'init': 'epsg:4617'}
        print(junctions)

        # https://gis.stackexchange.com/questions/311320/casting-geometry-to-multi-using-geopandas
        junctions["geometry"] = [MultiPoint([feature]) if type(feature) == Point else feature for feature in junctions["geometry"]]
        self.ferry_gdf["geometry"] = [MultiPoint([feature]) if type(feature) == Point else feature for feature in self.ferry_gdf["geometry"]]

        logging.info("Importing merged junctions into PostGIS.")
        junctions.postgis.to_postgis(con=engine, table_name='stage_2_junc', geometry='MULTIPOINT', if_exists='replace')
        self.ferry_gdf.postgis.to_postgis(con=engine, table_name='stage_2_ferry_junc', geometry='MULTIPOINT', if_exists='replace')

    def fix_junctype(self):
        """Generate attributes."""

        attr_fix = self.sql["attributes"]["query"]

        logging.info("Testing for junction equality and altering attributes.")
        attr_equality = gpd.GeoDataFrame.from_postgis(attr_fix, engine)

        print(attr_equality)

    def execute(self):
        """Executes an NRN stage."""

        self.load_gpkg()
        self.gen_dead_end()
        self.gen_intersections()
        self.gen_ferry()
        self.combine()
        self.fix_junctype()


def main():

    stage = Stage()
    stage.execute()


if __name__ == "__main__":

    main()

# output execution time
print("Total execution time: ", datetime.now() - startTime)