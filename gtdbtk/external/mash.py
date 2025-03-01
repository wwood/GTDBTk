import logging
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from typing import Tuple, Dict
from gtdbtk.biolib_lite.common import make_sure_path_exists
from gtdbtk.exceptions import GTDBTkExit
from gtdbtk.tools import tqdm_log
import gtdbtk.config.config as Config


class Mash(object):
    """Runs Mash against genomes."""

    def __init__(self, cpus, out_dir, prefix):
        """Instantiate the Mash class.

        Parameters
        ----------
        cpus : int
            The maximum number of CPUs available to Mash.
        out_dir : str
            The directory to write output files to.
        prefix : str
            The prefix for all output files.
        """
        self.logger = logging.getLogger('timestamp')
        self.cpus = max(cpus, 1)
        self.out_dir = out_dir
        self.prefix = prefix

    @staticmethod
    def version():
        """Returns the version of mash, or 'unknown' if not known."""
        try:
            proc = subprocess.Popen(['mash', '--version'], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, encoding='utf-8')
            stdout, stderr = proc.communicate()
            if len(stdout) > 0:
                return stdout.strip()
            else:
                return 'unknown'
        except Exception:
            return 'unknown'

    def run(self, qry, ref, mash_d, mash_k, mash_v, mash_s, mash_max_dist, mash_db) -> Dict[str, Dict[str, Tuple[float, float, int, int]]]:
        """Run Mash on a set of reference and query genomes.

        Parameters
        ----------
        qry : dict[str, str]
            The set of query genomes and their path.
        ref : dict[str, str]
            The set of reference genomes and their path.
        mash_d : float
            The maximum distance to report.
        mash_k : int
            The number of k-mers to store for each sequence.
        mash_v : float
            Maximum p-value to report.
        mash_s: int
            Maximum number of non-redundant hashes.
        max_mash_dist : float
            The maximum Mash distance to consider a genome as a close match.
        mash_db : Optional[str]
            The path to read/write the pre-computed Mash reference sketch database.

        Returns
        -------
        dict[query_id][ref_id] = (dist, p_val, shared_numerator, shared_denominator)
        """
        qry_sketch = QrySketchFile(qry, self.out_dir, self.prefix, self.cpus, mash_k, mash_s)
        ref_sketch = RefSketchFile(ref, self.out_dir, self.prefix, self.cpus, mash_k, mash_s, mash_db)

        # Generate an output file comparing the distances between these genomes.
        mash_dists = DistanceFile(qry_sketch, ref_sketch, self.out_dir, self.prefix,
                                  self.cpus, max_d=mash_d, mash_v=mash_v)
        results = mash_dists.read(mash_max_dist)

        # mash_db can be moved from filesystem to filesystem, so we need to update the path in the mash_db to
        # reflect the new location.
        current_ref = {os.path.basename(v): v for v in ref.values()}

        # Convert the results back to the accession
        path_to_qry = {v: k for (k, v) in qry.items()}
        path_to_ref = {v: k for (k, v) in ref.items()}
        out = defaultdict(dict)
        for qry_path, ref_hits in results.items():
            for ref_path, hit in ref_hits.items():
                current_ref_path = current_ref[ref_path]
                out[path_to_qry[qry_path]][path_to_ref[current_ref_path]] = hit
        return out


class DistanceFile(object):
    """The resulting distance file from the mash dist command."""
    name = 'mash_distances.tsv'

    def __init__(self, qry_sketch, ref_sketch, root, prefix, cpus, max_d, mash_v ,):
        """Create a new Mash distance file using these arguments.

        Parameters
        ----------
        qry_sketch : QrySketchFile
            The query sketch file generated by Mash.
        ref_sketch : RefSketchFile
            The reference sketch file generated by Mash.
        root : str
            The directory where the distance file will be contained.
        prefix : str
            The prefix to use in for distance file.
        cpus : int
            The maximum number of CPUs available to Mash.
        max_d : float
            The maximum distance to consider.
        mash_v : float
            The maximum value to consider.
        """
        self.logger = logging.getLogger('timestamp')
        self.qry_sketch = qry_sketch
        self.ref_sketch = ref_sketch
        self.path = os.path.join(root, f'{prefix}.{self.name}')
        self.cpus = cpus
        self.max_d = max_d
        self.mash_v = mash_v

        self._calculate()

    def _calculate(self):
        self.logger.info('Calculating Mash distances.')
        args = ['mash', 'dist', '-p', self.cpus, '-d', self.max_d, '-v',
                self.mash_v, self.ref_sketch.path, self.qry_sketch.path]
        args = list(map(str, args))
        with open(self.path, 'w') as f_out:
            proc = subprocess.Popen(args, stdout=f_out,
                                    stderr=subprocess.PIPE, encoding='utf-8')
            _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise GTDBTkExit(f'Error running Mash dist: {proc.stderr.read()}')

    def read(self,max_mash_dist=100) -> Dict[str, Dict[str, Tuple[float, float, int, int]]]:
        """Reads the results of the distance file.

        Returns
        -------
        dict[query_id][ref_id] = (dist, p_val, shared_numerator, shared_denominator)
        """
        out = defaultdict(dict)
        with open(self.path, 'r') as fh:
            hits = re.findall(r'(.+)\t(.+)\t(.+)\t(.+)\t(\d+)\/(\d+)\n', fh.read())
        for ref_id, qry_id, dist, p_val, shared_n, shared_d in hits:
            dist, p_val = float(dist), float(p_val)
            if dist <= max_mash_dist:
                shared_num, shared_den = int(shared_n), int(shared_d)
                out[qry_id][os.path.basename(ref_id)] = (dist, p_val, shared_num, shared_den)
        return out


class SketchFile(object):
    """Output files which are generated by mash sketch."""

    def __init__(self, genomes, path, cpus, k, s):
        """Create a sketch file for a given set of genomes.

        Parameters
        ----------
        genomes : dict[str, str]
            The genomes to create a sketch file from (genome_id, fasta_path).
        path : str
            The path to write the sketch file to.
        cpus : int
            The maximum number of CPUs available for Mash.
        k : int
            The k-mer size.
        s : int
            Maximum number of non-redundant hashes.
        """
        self.logger = logging.getLogger('timestamp')
        self.genomes = genomes
        self.path = path
        self.data = dict()
        self.args = dict()
        self.cpus = cpus
        self.k = k
        self.s = s

        make_sure_path_exists(os.path.dirname(self.path))

        # Use the pre-existing sketch file, otherwise generate it.
        if os.path.isfile(self.path):
            self.logger.info(f'Loading data from existing Mash sketch file: {self.path}')
            self._load_metadata()
            if not self._is_consistent():
                raise GTDBTkExit(f'The sketch file is not consistent with the '
                                 f'input genomes. Remove the existing sketch '
                                 f'file or specify a new output directory.')
        else:
            self.logger.info(f'Creating Mash sketch file: {self.path}')
            self._generate()

    def _load_metadata(self):
        """Loads the metadata from an existing Mash sketch file."""
        args = ['mash', 'info', '-t', self.path]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding='utf-8')
        stdout, stderr = proc.communicate()

        if proc.returncode != 0:
            raise GTDBTkExit(f'Error reading Mash sketch file {self.path}:\n{stderr}')

        for hashes, length, path in re.findall(r'(\d+)\t(\d+)\t(.+)\t.+\n', stdout):
            self.data[path] = (int(hashes), int(length))

    def _is_consistent(self):
        """Returns True if the sketch was generated from the genomes."""
        # to compare the consistency of the sketch file, we need to compare only the file names, not the full paths
        # the mash_db can be moves to another folder in the cloud so the full path will be different
        data_keys = set(map(os.path.basename,self.data.keys()))
        genomes_values = set(map(os.path.basename,self.genomes.values()))
        return data_keys == genomes_values

    def _generate(self):
        """Generate a new sketch file."""
        with tempfile.TemporaryDirectory(prefix='gtdbtk_mash_tmp_') as dir_tmp:
            path_genomes = os.path.join(dir_tmp, 'genomes.txt')
            with open(path_genomes, 'w') as fh:
                for path in self.genomes.values():
                    fh.write(f'{path}\n')

            args = ['mash', 'sketch', '-l', '-p', self.cpus, path_genomes, '-o',
                    self.path, '-k', self.k, '-s', self.s]
            args = list(map(str, args))
            proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, encoding='utf-8')
            with tqdm_log(total=len(self.genomes), unit='genome') as p_bar:
                for line in iter(proc.stderr.readline, ''):
                    if line.startswith('Sketching'):
                        p_bar.update()
            proc.wait()

            if proc.returncode != 0 or not os.path.isfile(self.path):
                raise GTDBTkExit(f'Error generating Mash sketch: {proc.stderr.read()}')


class QrySketchFile(SketchFile):
    name = 'user_query_sketch.msh'

    def __init__(self, genomes, root, prefix, cpus, k, s):
        """Create a query file for a given set of genomes.

        Parameters
        ----------
        genomes : dict[str, str]
            The genomes to create a sketch file from (genome_id, fasta_path).
        root : str
            The directory where the sketch file will be saved.
        prefix : str
            The prefix to use for this file.
        cpus : int
            The maximum number of CPUs available for Mash.
        k : int
            The k-mer size.
        s : int
            Maximum number of non-redundant hashes.
        """
        path = os.path.join(root, f'{prefix}.{self.name}')

        super().__init__(genomes, path, cpus, k, s)


class RefSketchFile(SketchFile):
    name = Config.MASH_SKETCH_FILE

    def __init__(self, genomes, root, prefix, cpus, k, s, mash_db=None):
        """Create a query file for a given set of genomes.

        Parameters
        ----------
        genomes : dict[str, str]
            The genomes to create a sketch file from (genome_id, fasta_path).
        root : str
            The directory where the sketch file will be saved.
        prefix : str
            The prefix to use for this file.
        cpus : int
            The maximum number of CPUs available for Mash.
        k : int
            The k-mer size.
        s : int
            Maximum number of non-redundant hashes.
        mash_db : Optional[str]
            The path to read/write the pre-computed Mash reference sketch database.
        """
        if mash_db is not None:
            export_msh = mash_db.rstrip('\\')
            if not export_msh.endswith(".msh"):
                export_msh = export_msh + ".msh"
            if os.path.isdir(export_msh):
                raise GTDBTkExit(f"{export_msh} is a directory")
            make_sure_path_exists(os.path.dirname(export_msh))
            path = export_msh
        else:
            path = os.path.join(root, f'{prefix}.{self.name}')

        super().__init__(genomes, path, cpus, k, s)
