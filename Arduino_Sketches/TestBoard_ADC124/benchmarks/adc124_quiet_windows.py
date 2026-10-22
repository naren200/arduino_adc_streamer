"""Detect coherent separated-state excursions; reject common sweep-time windows."""
import numpy as np

VERSION = 1


def between_bursts(t, samples, coarse):
    """Keep unimodal/isolated variation; mask coherent secondary-state excursions.

    A dominant one-code (>=60%) or adjacent two-code (>=80%) state anchors
    detection. Allow one count beyond its edge. An excursion direction is
    active only with >=1% mass beyond that edge and a separated histogram
    peak >=0.5% and >=3 times the edge density. Gaussian tails are not bursts.
    At least two routes must depart in the same sweep; reject the whole sweep
    and one neighboring sweep each side, identically for all channels.
    """
    t=np.asarray(t);samples=np.asarray(samples);coarse=np.asarray(coarse,dtype=bool)
    if samples.ndim!=2 or len(t)!=len(samples) or coarse.shape!=t.shape or not coarse.any():
        raise ValueError('Invalid/empty quiet-window input')
    count=np.zeros(len(t),dtype=np.int16);states=[];n=int(coarse.sum())
    for column in range(samples.shape[1]):
        codes=samples[:,column].astype(np.int64)
        hist=np.bincount(codes[coarse],minlength=4096)
        mode=int(hist.argmax());prob=float(hist[mode]/n);pair=int((hist[:-1]+hist[1:]).argmax())
        pair_prob=float((hist[pair]+hist[pair+1])/n)
        if prob>=.6: low,high=mode-1,mode+1;kind='one_code'
        elif pair_prob>=.8: low,high=pair-1,pair+2;kind='two_code'
        else:
            states.append(dict(column=column,active=False,mode_counts=mode,modal_mass=prob,pair_mass=pair_prob));continue
        low=max(low,0);high=min(high,4095)
        def separated(edge,tail):
            return bool(len(tail) and tail.sum()>=.01*n and tail.max()>=max(.005*n,3*hist[edge]))
        down=separated(low,hist[:low]);up=separated(high,hist[high+1:])
        hit=((codes<low) if down else np.zeros(len(t),bool))|((codes>high) if up else np.zeros(len(t),bool))
        count+=hit
        states.append(dict(column=column,active=bool(down or up),kind=kind,mode_counts=mode,modal_mass=prob,pair_mass=pair_prob,
            lower_edge_counts=low,upper_edge_counts=high,detect_lower=down,detect_upper=up,
            excursion_fraction=float(hit[coarse].mean())))
    coherent=coarse&(count>=2);reject=coherent.copy()
    reject[1:]|=coherent[:-1];reject[:-1]|=coherent[1:]
    mask=coarse&~reject
    period=float(np.median(np.diff(t)))
    weights=np.minimum(np.r_[np.diff(t),period],period)
    return dict(mask=mask,coherent=coherent,reject=reject,states=states,version=VERSION,
        quiet_seconds=float(weights[mask].sum()),coarse_observed_seconds=float(weights[coarse].sum()),
        fine_excursion_frames=int(coherent.sum()),fine_rejected_frames=int((coarse&reject).sum()),
        fine_rejected_fraction=float((coarse&reject).sum()/coarse.sum()))
