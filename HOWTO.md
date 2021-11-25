## How does it work ?

Every 20 minutes, we enqueue a job to get all the new changesets in Mercurial repository and to get all the new crashes on Socorro, we gather them by signatures and proto-signatures and store them in a table in the database. Only signatures which were not present in the last 3 days are retrieved: we try to get only new signatures to avoid to have too much noise.
Once this job is done, we enqueue some jobs to get and parse patch for each revision: we extracted and store the added, removed, modified line numbers for each files touched in the patch.
Then we enqueue jobs to get a json file for each crash containing the stack of the crash: the stack contains filenames and line numbers where a function has been called.
Once we have the stack, it's easy to compare line numbers with the Mercurial history and we can say if a line belonging to the stack has been touched recently by a patch.
Sometimes a modification on line before or after the line in stack can have an effect on it: for example
```C
char * x = malloc(123);
free(x);
x[3] = 'x';
```
if the 2nd line has been added in a patch, then it'll induce a crash at the 3rd line.
So in order to have a clue, we compute a number to measure the proximity of the line in stack with the set of lines in the patch: this is the score.
If the score is 10 then it means that the line in stack has been touched by a patch, if it is 9, then it's very close, ...

## How to use the amazing UI ?

![Interesting](/images/interesting.png)

The score for `mozilla::net::Http2Session::SanityCheck` is 10 (there is a gradient of colors between 0 and 10 from red to green) which looks promising.
This signature isn't current (not like the OOM one), the score is 10 so very likely we'll get something interesting.
So we can click on the crash UUID on the right to get:

![Stack](/images/stack.png)

For each element on the stack, we have the patches information in the last column: changeset, score, patch pushdate, bug number and if the patch has been backed out.
So here we can see that `c376158bf365` is interesting so we can click on the magnifying glass to get a popup menu: most of the time the submenu `Open file|diff` is useful, so we can click on it to get

![Compare](/images/compare.png)

Here the line number in the stack is 531 (blue line on the left) so we need to scroll to find hunk containing this line in the patch (on the right).
It's easy to see that the patch is the culprit: an assertion has been added to help to debug \o/.

We must take care that the score only reflect the proximity of the guilty line and the touched lines in the patch which means that some other patches may have changed the position of a potential guilty line. But the different patchs are sorted from the most recent (top) to the less recent (bottom).

Another important is to read the code, to check for example that the patch author didn't just add a comment: the change must be relevant.

Once we've the culprit, we can then file a bug or just add a comment in the corresponding bug (we've a link in the stack view), it depends:
 - if the bug is still open and especially if it's an assertion, a comment in the existing bug is enough (don't forget to add the signature in the bug report);
 - else we must open a new bug and it can be done in using the magnifying glass button and the submenu `Report a bug`.

 ![Report](/images/report.png)

Clicking on `Create a new bug` will open a prefiled bug in Bugzilla, but since the needinfo cannot be prefiled, we must copy the email of the author of the patch.
It's up to you to check that the already existing bugs should be used or not.

So now we're on Bugzilla, we must copy the bug number in `Blocks` into `Regressed by`

![Blocks](/images/blocks.png)

and we must set status flags and a needinfo for the author:

![Status](/images/status.png)

## Few remarks.


### Several stacks

When there are several stacks (last column)

![Several Stacks](/images/several.png)

there is a stack by proto-signatures: sometimes we've some crashes with the same final signature but the "path" which triggered it is different.


### Common functions

There are some very common functions which are in a lot of stacks, so obviously, when the file containing them are touched we can have a lot of new crashes in the UI but in general the "guilty" patch is innocent.


### Experience

With time, we learnt that most of the time crashes involving for example some changes in js engine are innocent even if they look guilty.
