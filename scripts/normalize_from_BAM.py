#! /usr/bin/python

"""
reads a BAM file extracts valid pairs, and from them, computes an array of 
biases (1 round ICE) and array of normalized expected counts according to 
genomic distance, and an array of columns with poor_bins signal.
"""

from collections                  import OrderedDict
from pytadbit.utils.file_handling import mkdir
from pytadbit.utils.extraviews    import nicer
from cPickle                      import dump, load
from time                         import sleep, time
from argparse                     import ArgumentParser
import sys, os
import pysam
import multiprocessing  as mu
import datetime


def printime(msg):
    print (msg +
           (' ' * (79 - len(msg.replace('\n', '')))) +
           '[' +
           str(datetime.datetime.fromtimestamp(time()).strftime('%Y-%m-%d %H:%M:%S')) +
           ']')


def read_bam_frag(inbam, filter_exclude, sections, bin2crm,
                  resolution, outdir, region, start, end):
    bamfile = pysam.AlignmentFile(inbam, 'rb')
    refs = bamfile.references
    try:
        dico = {}
        for r in bamfile.fetch(region=region,
                               start=start - (1 if start else 0), end=end,  # coords starts at 0
                               multiple_iterators=True):
            if r.flag & filter_exclude:
                continue
            crm1 = r.reference_name
            pos1 = r.reference_start + 1
            crm2 = refs[r.mrnm]
            pos2 = r.mpos + 1
            try:
                pos1 = sections[(crm1, pos1 / resolution)]
                pos2 = sections[(crm2, pos2 / resolution)]
            except KeyError:
                continue  # not in the subset matrix we want
            try:
                dico[(pos1, pos2)] += 1
            except KeyError:
                dico[(pos1, pos2)] = 1
        sumcol = {}
        sumdia = {}
        for (i, j), v in dico.iteritems():
            # out.write('%d\t%d\t%d\n' % (i, j, v))
            try:
                sumcol[i] += v
            except KeyError:
                sumcol[i]  = v
            # we only measure genomic distance if same chromsome
            if bin2crm[i] != bin2crm[j]:
                continue
            l = abs(j - i)
            try:
                sumdia[l].append(v)
            except KeyError:
                sumdia[l] = [v]
        out = open(os.path.join(outdir,
                                'tmp_%s:%d-%d.pickle' % (region, start, end)), 'w')
        dump(dico, out)
        out.close()
    except Exception, e:
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        print e
        print(exc_type, fname, exc_tb.tb_lineno)
    return sumcol, sumdia


def print_progress(procs):
    sys.stdout.write('     ')
    prev_done = done = i = 0
    while done < len(procs):
        sleep(2)
        done = sum(p.ready() for p in procs)
        for i in range(prev_done, done):
            if not i % 10 and i:
                sys.stdout.write(' ')
            if not i % 50 and i:
                sys.stdout.write(' %9s\n     ' % ('%s/%s' % (i , len(procs))))
            sys.stdout.write('.')
            sys.stdout.flush()
        prev_done = done    
    print '%s %9s\n' % (' ' * (54 - (i % 50) - (i % 50) / 10),
                        '%s/%s' % (len(procs),len(procs)))


def read_bam(inbam, filter_exclude, resolution, min_count=2500,
             ncpus=8, factor=1, outdir='.'):
    bamfile = pysam.AlignmentFile(inbam, 'rb')
    sections = OrderedDict(zip(bamfile.references,
                               [x / resolution + 1 for x in bamfile.lengths]))
    total = 0
    section_pos = dict()
    for crm in sections:
        section_pos[crm] = (total, total + sections[crm])
        total += sections[crm] + 1
    bins = []
    for crm in sections:
        len_crm = sections[crm]
        bins.extend([(crm, i) for i in xrange(len_crm + 1)])

    start_bin = 0
    end_bin   = len(bins) + 1
    total = len(bins)

    total = end_bin - start_bin + 1
    regs = []
    begs = []
    ends = []
    njobs = min(total, 100) + 1
    nbins = total / njobs + 1
    for i in range(start_bin, end_bin, nbins):
        if i + nbins > end_bin:  # make sure that we stop at the right place
            nbins = end_bin - i
        try:
            (crm1, beg1), (crm2, end2) = bins[i], bins[i + nbins - 1]
        except IndexError:
            (crm1, beg1), (crm2, end2) = bins[i], bins[-1]
        if crm1 != crm2:
            end1 = sections[crm1]
            beg2 = 0
            regs.append(crm1)
            regs.append(crm2)
            begs.append(beg1 * resolution)
            begs.append(beg2 * resolution)
            ends.append(end1 * resolution + resolution)  # last nt included
            ends.append(end2 * resolution + resolution - 1)  # last nt not included (overlap with next window)
        else:
            regs.append(crm1)
            begs.append(beg1 * resolution)
            ends.append(end2 * resolution + resolution - 1)            
    ends[-1] += 1  # last nucleotide included

    # print '\n'.join(['%s %d %d' % (a, b, c) for a, b, c in zip(regs, begs, ends)])
    printime('\n  - Parsing BAM (%d chunks)' % (len(regs)))
    bins_dict = dict([(j, i) for i, j in enumerate(bins)])
    bin2crm = dict((v, k[0]) for k, v in bins_dict.iteritems())
    pool = mu.Pool(ncpus)
    procs = []
    for i, (region, start, end) in enumerate(zip(regs, begs, ends)):
        procs.append(pool.apply_async(
            read_bam_frag, args=(inbam, filter_exclude, bins_dict, bin2crm,
                                 resolution, outdir, region, start, end,)))
    pool.close()
    print_progress(procs)
    pool.join()

    ## COLLECT RESULTS
    colsum = {}
    diasum = {}
    for p in procs:
        c, d = p.get()
        colsum.update(c)
        for k, v in d.iteritems():
            try:
                diasum[k].extend(v)
            except KeyError:
                diasum[k] = v
    for k in diasum:
        diasum[k] = float(sum(diasum[k])) / len(diasum[k])
    # bad columns
    badcol = {}
    for c in xrange(total):
        if colsum.get(c, 0) < min_count:
            badcol[c] = colsum.get(c, 0)

    printime('  - Rescaling biases')
    size = len(bins)
    biases = [colsum.get(k, 1.) for k in range(size)]
    mean_col = float(sum(biases)) / len(biases)
    biases = dict([(k, b / mean_col * mean_col**0.5) for k, b in enumerate(biases)])

    # collect subset-matrices and write genomic one
    # out = open(os.path.join(outdir,
    #                         'hicdata_%s.abc' % (nicer(resolution).replace(' ', ''))), 'w')
    pool = mu.Pool(ncpus)
    procs = []
    for i, (region, start, end) in enumerate(zip(regs, begs, ends)):
        fname = os.path.join(outdir, 'tmp_%s:%d-%d.pickle' % (region, start, end))
        procs.append(pool.apply_async(sum_nrm_matrix, args=(fname, biases, )))
    pool.close()
    print_progress(procs)
    pool.join()

    # to correct biases
    sumnrm = sum(p.get() for p in procs)

    target = (sumnrm / float(size * size * factor))**0.5
    biases = dict([(b, biases[b] * target) for b in biases])

    printime('  - Rescaling decay')
    # normalize decay by size of the diagonal, and by Vanilla correction
    # (all cells must still be equals to 1 in average)
    # for d in xrange(max(diasum)):
    #     try:
    #         diasum[d] /= float(size - d)
    #     except ZeroDivisionError:
    #         diasum[d] = 1.
    #     except KeyError:
    #         diasum[d] = 1.

    pool = mu.Pool(ncpus)
    procs = []
    for i, (region, start, end) in enumerate(zip(regs, begs, ends)):
        fname = os.path.join(outdir, 'tmp_%s:%d-%d.pickle' % (region, start, end))
        procs.append(pool.apply_async(sum_dec_matrix, args=(fname, biases, diasum, )))
    pool.close()
    print_progress(procs)
    pool.join()

    # to correct decay
    sumdec = {}
    for proc in procs:
        for k, v in proc.get().iteritems():
            try:
                sumdec[k].extend(v)
            except KeyError:
                sumdec[k] = v
    for k in sumdec:
        sumdec[k] = float(sum(sumdec[k])) / len(sumdec[k])
    # divide by the average of the diagonals and multiply by two, because 1 diag per half square
    target = dict([(d, sumdec[d] / float((total - d) * factor * 2))
                   for d in sumdec])
    diasum = dict([(d, diasum[d] * target[d]) for d in diasum])

    return biases, diasum, badcol


def sum_nrm_matrix(fname, biases):
    dico = load(open(fname))
    sumnrm = sum(v / (biases[i] * biases[j])
                 for (i, j), v in dico.iteritems())
    return sumnrm


def sum_dec_matrix(fname, biases, decay):
    dico = load(open(fname))
    sumdec = {}
    for (i, j), v in dico.iteritems():
        k = abs(i-j)
        try:
            sumdec[k].append(v / (biases[i] * biases[j] * decay[k]))
        except KeyError:
            try:
                sumdec[k] = [v / (biases[i] * biases[j] * decay[k])]
            except KeyError:
                pass
    os.system('rm -f %s' % (fname))
    return sumdec


def main():
    opts          = get_options()

    inbam          = opts.inbam
    resolution     = opts.reso
    filter_exclude = opts.filter
    min_count      = opts.min_count
    ncpus          = opts.cpus
    factor         = 1
    outdir         = opts.outdir
    
    mkdir(outdir)
    
    sys.stdout.write('\nNormalization of full genome\n')

    biases, decay, badcol = read_bam(inbam, filter_exclude, resolution,
                                     min_count=min_count, ncpus=ncpus,
                                     factor=factor, outdir=outdir)
    
    printime('  - Saving biases and badcol columns')
    # biases
    out = open(os.path.join(outdir, 'biases_%s.pickle' % (
        nicer(resolution).replace(' ', ''))), 'w')
    
    dump({'biases'    : biases,
          'decay'     : decay,
          'badcol'    : badcol,
          'resolution': resolution}, out)
    out.close()
    
    # hic_data.write_matrix('chr_names%s_%d-%d.mat' % (region, start, end), focus=())
    printime('\nDone.')


def get_options():
    parser = ArgumentParser(usage="%(prog)s -i PATH -r INT [options]")

    parser.add_argument('-i', '--infile', dest='inbam', metavar='',
                        required=True, default=False, help='input HiC-BAM file.')
    parser.add_argument('-o', '--outdir', dest='outdir', metavar='',
                        required=True, default=False, help='output directory.')
    parser.add_argument('-r', '--resolution', dest='reso', type=int, metavar='',
                        required=True, help='''wanted resolution form th generated matrix''')
    parser.add_argument('--min_count', dest='min_count', type=int, metavar='',
                        default=2500,
                        help='''[%(default)s] minimum number of interactions perc_zero bin''')
    parser.add_argument('-C', '--cpus', dest='cpus', metavar='', type=int,
                        default=8, help='''[%(default)s] number of cpus to be 
                        used for parsing the HiC-BAM file''')
    parser.add_argument('-F', '--filter', dest='filter', metavar='', type=int,
                        default=391, help='''[%(default)s] binary code to 
                        exclude filtered reads [OPTION TO BE IMPROVED]''')
    
    opts = parser.parse_args()
    
    return opts


if __name__=='__main__':
    exit(main())