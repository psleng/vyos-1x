<!-- include start from serial/general/flow-control-none-and-software.xml.i -->
<node name="flow-control">
  <properties>
    <help>Flow control</help>
  </properties>
  <children>
    <leafNode name="none">
      <properties>
        <help>No flow control (default)</help>
        <valueless/>
      </properties>
    </leafNode>
    <node name="software">
      <properties>
        <help>Turn on software flow control only</help>
      </properties>
      <children>
        #include <include/serial/general/flow-control-direction.xml.i>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
