import click
import fiona
import geopandas as gpd
import logging
import sys
from itertools import chain
from pathlib import Path
from tabulate import tabulate

filepath = Path(__file__).resolve()
sys.path.insert(1, str(filepath.parents[1]))
import helpers
from validation_functions import Validator


# Set logger.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(handler)


class CRNMeshblockValidation:
    """Defines the CRN meshblock validation class."""

    def __init__(self, source: str, export_meshblock: bool = False) -> None:
        """
        Initializes the CRN class.

        :param str source: abbreviation for the source province / territory.
        :param bool export_meshblock: export meshblock polygon layer.
        """

        self.source = source
        self.layer = f"nrn_bo_{source}"
        self.id = "segment_id"
        self.export_meshblock = export_meshblock
        self.Validator = None
        self.src = Path(filepath.parents[2] / "data/egp_data.gpkg")
        self.src_restore = Path(filepath.parents[2] / "data/nrn_bo_restore.gpkg")

        # Configure source path and layer name.
        for src in (self.src, self.src_restore):
            if src.exists():
                if self.layer not in set(fiona.listlayers(src)):
                    logger.exception(f"Layer \"{self.layer}\" not found within source: \"{src}\".")
                    sys.exit(1)
            else:
                logger.exception(f"Source not found: \"{src}\".")
                sys.exit(1)

        # Load source data.
        logger.info(f"Loading source data: {self.src}|layer={self.layer}.")
        self.crn = gpd.read_file(self.src, layer=self.layer)
        logger.info("Successfully loaded source data.")

        # Standardize data and snap nodes.
        self.crn = helpers.standardize(self.crn)
        self.crn = helpers.snap_nodes(self.crn)

    def __call__(self) -> None:
        """Executes the CRN class."""

        self.validations()
        self.write_errors()

    def write_errors(self) -> None:
        """Writing error flags returned by validations to DataFrame columns."""

        logger.info(f"Writing error flags to dataset: \"{self.layer}\".")

        # Quantify errors.
        identifiers = list(chain.from_iterable(self.Validator.errors.values()))
        total_records = len(identifiers)
        total_unique_records = len(set(identifiers))

        # Iterate and write errors to DataFrame.
        for code, vals in sorted(self.Validator.errors.items()):

            self.crn[f"v{code}"] = self.crn[self.id].isin(vals).astype(int)

            logger.info(f"Total records flagged by validations: {total_records:,d}.")
            logger.info(f"Total unique records flagged by validations: {total_unique_records:,d}.")

        # Export data.
        helpers.export(self.crn, dst=self.src, name=self.layer)
        if self.export_meshblock:
            helpers.export(self.Validator.meshblock_, dst=self.src, name=f"meshblock_{self.source}")

        # Log meshblock creation progress.
        table = tabulate([[k, f"{v:,}"] for k, v in self.Validator.meshblock_progress.items()],
                         headers=["Meshblock Input Arcs", "Count"], tablefmt="rst", colalign=("left", "right"))
        logger.info("\n" + table)

    def validations(self) -> None:
        """Applies a set of validations to BOs."""

        logger.info("Initiating validator.")

        # Instantiate and execute validator class.
        self.Validator = Validator(self.crn, dst=self.src, layer=self.layer)
        self.Validator()


@click.command()
@click.argument("source", type=click.Choice(helpers.load_yaml("../config.yaml")["sources"], False))
@click.option("--export-meshblock / --no-export-meshblock", "-e", default=False, show_default=True,
              help="Export meshblock polygon layer.")
def main(source: str, export_meshblock: bool = False) -> None:
    """
    Instantiates and executes the CRN class.

    :param str source: abbreviation for the source province / territory.
    :param bool export_meshblock: export meshblock polygon layer.
    """

    try:

        with helpers.Timer():
            crn = CRNMeshblockValidation(source, export_meshblock)
            crn()

    except KeyboardInterrupt:
        logger.exception("KeyboardInterrupt: Exiting program.")
        sys.exit(1)


if __name__ == "__main__":
    main()
