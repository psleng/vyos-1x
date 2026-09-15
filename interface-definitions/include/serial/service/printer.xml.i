<!-- include start from serial/service/printer.xml.i -->
<node name="remote-printer">
  <properties>
    <help>Remote-printer service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/transmit-string-no-end.xml.i>
    <leafNode name="map-cr-to-crlf">
      <properties>
        <help>Enable mapping carriage returns (CR) to carriage return line feed (CRLF)</help>
        <valueless/>
      </properties>
    </leafNode>
  </children>
</node>
<!-- include end -->
